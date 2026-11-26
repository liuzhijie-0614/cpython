"""Check a standalone install using that install's isolated interpreter."""

import re
import subprocess
import sys
import sysconfig
from importlib import metadata
from pathlib import Path


def check_equal(label, actual, expected):
    if actual != expected:
        raise SystemExit(f"{label}: expected {expected!r}, got {actual!r}")


def check_git(source):
    if not (source / ".git").exists():
        return
    _, identifier, compiled_revision = sys._git
    if not compiled_revision or not identifier:
        raise SystemExit(
            "CPython Git metadata is missing; the compilation image must provide Git"
        )

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", f"safe.directory={source}", "-C", source, *args],
            text=True,
        ).strip()

    revision = git("rev-parse", "HEAD")
    if not revision.startswith(compiled_revision):
        raise SystemExit(
            f"CPython Git revision: expected a prefix of {revision!r}, "
            f"got {compiled_revision!r}"
        )
    identifier_in_source = git("describe", "--all", "--always")
    # CPython's build uses describe --dirty. PBS patches its source copy, so
    # the compiled identifier may add -dirty even for a clean source checkout.
    if identifier not in {identifier_in_source, identifier_in_source + "-dirty"}:
        raise SystemExit(
            f"CPython Git identifier: expected {identifier_in_source!r} "
            f"(optionally with '-dirty'), got {identifier!r}"
        )
    print(f"Source Git: {identifier_in_source}:{revision}")
    print(f"CPython Git: {identifier}:{compiled_revision}")


def check_distribution(prefix, name):
    distribution = metadata.distribution(name)
    location = Path(distribution.locate_file("")).resolve()
    if not location.is_relative_to(prefix):
        raise SystemExit(f"{name} is outside the install tree: {location}")
    print(f"{name} {distribution.version}")
    return distribution


def check_requirements(prefix, source):
    from packaging.requirements import Requirement

    # This repository uses one pinned requirement per line. Verify those pins
    # without maintaining a second package list in this script.
    for line in (source / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        distribution = check_distribution(prefix, requirement.name)
        if not requirement.specifier.contains(
            distribution.version, prereleases=True
        ):
            raise SystemExit(
                f"Expected {requirement}, got {distribution.version}"
            )


def main():
    prefix = Path(sys.argv[1]).resolve()
    expected_version, options = sys.argv[2], set(sys.argv[3].split("+"))
    source = Path(sys.argv[4])
    install_requirements = sys.argv[6] == "true"

    check_equal("Python version", sys.version.split()[0], expected_version)
    check_git(source)

    # configure takes the last BUILD_VERSION component as PY_MICRO_BYTEDVERSION.
    # Read the remaining components from the source snapshot used for this build.
    expected_byted_version = expected_version
    if build_version := sys.argv[5]:
        header = source / "Include" / "pybyted.h"
        defines = dict(
            re.findall(
                r"^\s*#define\s+(\w+)\s+(\S+)",
                header.read_text(),
                re.MULTILINE,
            )
        )
        expected_byted_version = ".".join((
            expected_version,
            defines["BM_SEPARATOR"].strip('"'),
            defines["PY_MAJOR_BYTEDVERSION"],
            defines["PY_MINOR_BYTEDVERSION"],
            build_version.rsplit(".", 1)[-1],
        ))
    for name in ("python3", "python"):
        for flag, expected in (
            ("--version", expected_version),
            ("--BV", expected_byted_version),
            ("-VV", expected_version),
        ):
            output = subprocess.check_output(
                [prefix / "bin" / name, "-I", flag], text=True
            ).strip()
            match = re.match(r"^Python (\S+)(?:\s|$)", output)
            if match is None:
                raise SystemExit(f"Unexpected {name} {flag} output: {output!r}")
            check_equal(f"{name} {flag}", match[1], expected)
            if flag == "-VV":
                check_equal(f"{name} {flag}", output, f"Python {sys.version}")
            print(output)

    check_equal("Install prefix", Path(sys.prefix).resolve(), prefix)
    check_equal("Base prefix", Path(sys.base_prefix).resolve(), prefix)
    check_equal(
        "Interpreter directory",
        Path(sys.executable).resolve().parent,
        prefix / "bin",
    )
    for name, option in (
        ("Py_GIL_DISABLED", "freethreaded"),
        ("Py_DEBUG", "debug"),
    ):
        check_equal(
            name,
            int(sysconfig.get_config_var(name) or 0),
            int(option in options),
        )

    check_distribution(prefix, "pip")
    if install_requirements:
        check_requirements(prefix, source)


if __name__ == "__main__":
    main()
