#!/usr/bin/env python3
"""Build this checkout with python-build-standalone. Enter via build_standalone.sh."""

import fcntl
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from standalone_docker import (
    DockerError,
    DockerPermissionError,
    ensure_docker,
    ignore_interrupts,
)


class BuildError(Exception):
    """A configuration or build failure with a user-facing explanation."""


def log(message):
    print(f"\n[standalone] {message}", flush=True)


def run(args, *, cwd=None, env=None, capture=False, quiet=False):
    # Give each command a process group so interruption also reaches uv/Make's
    # descendants. Inherit stdin and output for the normal build commands.
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        start_new_session=True,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL if quiet else None,
    )
    try:
        output, _ = process.communicate()
    except BaseException:
        with ignore_interrupts():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, args)
    return output.strip() if capture else None


@dataclass(frozen=True)
class Config:
    source: Path
    work_dir: Path
    branch: str
    output: Path
    options: str
    target: str
    python_version: str
    release_tag: str
    build_version: str
    install_requirements: bool = False

    @classmethod
    def from_environment(cls):
        source = Path(__file__).resolve().parent.parent
        work_dir = Path(
            os.environ.get("CUSTOM_STANDALONE_WORK_DIR")
            or source.parent / ".cpython-standalone"
        ).resolve()
        output = Path(
            os.environ.get("STANDALONE_OUTPUT_DIR") or source / "output"
        ).resolve()
        options = os.environ.get("CUSTOM_COMPILER_OPTIONS") or "pgo+lto"
        if options.startswith("free-threaded+"):
            options = "freethreaded+" + options[len("free-threaded+") :]
        optimization = options.removeprefix("freethreaded+")
        if optimization not in {"noopt", "debug", "pgo", "lto", "pgo+lto"}:
            raise BuildError(
                f"Unsupported CUSTOM_COMPILER_OPTIONS: {options}. "
                "Use --help for supported combinations."
            )
        if platform.system() != "Linux":
            raise BuildError(
                "Standalone builds require a Linux host or CI environment."
            )
        machine = platform.machine()
        if machine not in {"x86_64", "aarch64"}:
            raise BuildError("Supported host architectures are x86_64 and aarch64.")
        header = (source / "Include/patchlevel.h").read_text()
        match = re.search(r'^\s*#define\s+PY_VERSION\s+"([^"]+)"', header, re.MULTILINE)
        if match is None or not re.match(r"^\d+\.\d+\.\d+", match[1]):
            raise BuildError("Cannot read PY_VERSION from Include/patchlevel.h.")
        release_tag = os.environ.get("PYBUILD_RELEASE_TAG") or datetime.now(
            timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", release_tag):
            raise BuildError("PYBUILD_RELEASE_TAG must be a filename-safe tag.")
        return cls(
            source=source,
            work_dir=work_dir,
            branch=os.environ.get("CUSTOM_STANDALONE_BUILD_BRANCH") or "release",
            output=output,
            options=options,
            target=f"{machine}-unknown-linux-gnu",
            python_version=match[1],
            release_tag=release_tag,
            build_version=os.environ.get("BUILD_VERSION", ""),
            install_requirements=os.environ.get("CUSTOM_INSTALL_REQUIREMENTS")
            == "true",
        )

    @property
    def builder(self):
        return (self.work_dir / "python-build-standalone").resolve()

    @property
    def work(self):
        return (self.work_dir / "work").resolve()

    def build_environment(self):
        env = os.environ.copy()
        env["PYBUILD_PYTHON_VERSION"] = self.python_version
        env["PYBUILD_RELEASE_TAG"] = self.release_tag
        return env


def check_environment(config):
    for tool in ("python3", "uv", "docker", "make", "tar"):
        if shutil.which(tool) is None:
            raise BuildError(
                f"Missing {tool}; install it and add it to PATH before building."
            )
    for args in (
        ["python3", "--version"],
        ["uv", "--version"],
        ["docker", "--version"],
    ):
        run(args)
    try:
        run(["docker", "buildx", "version"])
    except subprocess.CalledProcessError as error:
        raise BuildError(
            "Docker Buildx is required; install the Docker Buildx plugin."
        ) from error


def check_paths(config):
    if any(
        config.source.is_relative_to(path) or path.is_relative_to(config.source)
        for path in (config.work_dir, config.builder, config.work)
    ):
        raise BuildError("The standalone work directory must be outside the CPython checkout.")
    if (
        any(
            path.is_relative_to(config.output)
            for path in (config.source, config.work_dir)
        )
        or config.output.is_relative_to(config.builder)
        or config.work.is_relative_to(config.output)
    ):
        raise BuildError(
            f"Output conflicts with the source or builder directory: {config.output}"
        )
    if (
        config.output.exists()
        and config.output != config.source / "output"
        and not (config.output / ".standalone-build-output").is_file()
    ):
        raise BuildError(f"Refusing to replace unmarked custom output: {config.output}")
    if os.environ.get("PYBUILD_NO_DOCKER"):
        raise BuildError(
            "Unset PYBUILD_NO_DOCKER; this script uses the builder's Docker toolchain."
        )


def prepare_builder(config):
    if not (config.builder / ".git").exists():
        log(f"Clone builder branch {config.branch} into {config.builder}")
        run(
            [
                "git",
                "clone",
                "--single-branch",
                "--branch",
                config.branch,
                "--",
                "git@code.byted.org:lang/python-build-standalone.git",
                config.builder,
            ]
        )

    def git(*args, capture=False):
        # Elevated builds can reuse a checkout owned by the original caller.
        return run(
            [
                "git",
                "-c",
                f"safe.directory={config.builder}",
                "-C",
                config.builder,
                *args,
            ],
            capture=capture,
        )

    branch = git("rev-parse", "--abbrev-ref", "HEAD", capture=True)
    if branch != config.branch:
        log(f"Switch builder branch from {branch} to {config.branch}")
        if not git("branch", "--list", "--", config.branch, capture=True):
            # Extend a single-branch clone to fetch the selected branch.
            refspec = f"+refs/heads/{config.branch}:refs/remotes/origin/{config.branch}"
            git("config", "--add", "remote.origin.fetch", refspec)
            git("fetch", "origin", refspec)
            git("branch", "--track", config.branch, f"origin/{config.branch}")
        # Stash tracked edits while keeping untracked build caches in place.
        if git("status", "--porcelain", "--untracked-files=no", capture=True):
            git(
                "-c",
                "user.name=Standalone build",
                "-c",
                "user.email=standalone-build@localhost",
                "stash",
                "push",
                "-m",
                f"standalone build: changes on {branch}",
            )
            saved = git("rev-parse", "refs/stash", capture=True)
            log(f"Saved local builder changes from {branch} in Git stash {saved}")
        git("checkout", config.branch)
    commit = git("rev-parse", "--short", "HEAD", capture=True)
    log(f"Builder: {config.builder} (branch {config.branch}, commit {commit})")


@contextmanager
def workspace(config):
    with ExitStack() as stack:

        def acquire_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            # flock does not write the file; root-owned cache locks can be read by the caller.
            lock = stack.enter_context(path.open("r" if path.exists() else "a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise BuildError(
                    f"Another standalone build is using {config.builder}."
                ) from error

        # Match flock's advisory locks, including the legacy lock, and hold both
        # until cleanup finishes. The outer lock also protects the first clone.
        acquire_lock(Path(f"{config.builder}.lock"))
        prepare_builder(config)
        acquire_lock(config.builder / "build/.cpython-standalone.lock")
        config.work.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="cpython-standalone.", dir=config.work))
        try:
            yield scratch
        finally:
            with ignore_interrupts():
                shutil.rmtree(scratch)


def build_distribution(config, scratch):
    args = [
        "uv",
        "run",
        "--no-dev",
        "build.py",
        "--python",
        f"cpython-{config.python_version.rsplit('.', 1)[0]}",
        "--python-source",
        config.source,
        "--target-triple",
        config.target,
        "--options",
        config.options,
        # We publish build/*.tar's install tree; PBS's extra compressed archive is only a cache.
        "--compression",
        "fast",
    ]
    if config.build_version:
        args.extend(["--build-version", config.build_version])
    # Share the daemon host's network for image RUN steps, including nested CI.
    # PBS has no image-network option. Interpose a CLI wrapper for Buildx only.
    docker_bin = scratch / "docker-bin"
    docker_bin.mkdir()
    wrapper = docker_bin / "docker"
    wrapper.write_text("""#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == buildx && ${2:-} == build ]]; then
    shift 2
    exec "${STANDALONE_DOCKER_CLI}" buildx build --network=host "$@"
fi
exec "${STANDALONE_DOCKER_CLI}" "$@"
""")
    wrapper.chmod(0o755)
    env = config.build_environment()
    env["STANDALONE_DOCKER_CLI"] = shutil.which("docker")
    env["PATH"] = f"{docker_bin}:{env['PATH']}"
    # Local edits and BUILD_VERSION are not Make prerequisites. Rebuild both
    # interpreters while retaining dependency and toolchain caches.
    env["MAKEFLAGS"] = env.get("MAKEFLAGS", "") + "".join(
        f" --assume-new={config.builder}/cpython-unix/{name}"
        for name in ("build-cpython.sh", "build-cpython-host.sh")
    )
    log("Docker image build network: host")
    run(args, cwd=config.builder, env=env)


def extract_distribution(config, scratch):
    archive = (
        config.builder
        / "build"
        / (f"cpython-{config.python_version}-{config.target}-{config.options}.tar")
    )
    if not archive.is_file():
        raise BuildError(f"Builder did not produce {archive}.")
    # Keep GNU tar's handling of PBS links and metadata. Only install/ is published.
    run(
        [
            "tar",
            "-xf",
            archive,
            "-C",
            scratch,
            "python/PYTHON.json",
            "python/install",
        ]
    )


def verify_distribution(config, scratch):
    env = config.build_environment()
    env.pop("TERM", None)  # Skip the curses test in this non-interactive build.
    run(
        ["uv", "run", "--no-dev", "test-distribution.py", scratch / "python"],
        cwd=config.builder,
        env=env,
    )


def install_requirements(config, scratch):
    install = scratch / "python/install"
    run(
        [
            install / "bin/python3",
            "-I",
            "-m",
            "pip",
            "install",
            "--no-compile",
            "-r",
            config.source / "requirements.txt",
        ]
    )
    # Match PBS's relative shebangs for console scripts after relocation.
    for path in (install / "bin").iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            first = stream.readline(512)
            if not first.startswith(b"#!") or b"python" not in first:
                continue
            contents = stream.read()
        path.write_bytes(
            b"#!/bin/sh\n"
            b'\'\'\'exec\' "$(dirname -- "$(realpath -- "$0")")/python3" "$0" "$@"\n'
            b"' '''\n" + contents
        )


def verify_install(config, source, prefix):
    # Run all Python checks with the built interpreter, never the build driver.
    run(
        [
            prefix / "bin/python3",
            "-I",
            source / "standalone/verify_standalone_install.py",
            prefix,
            config.python_version,
            config.options,
            source,
            config.build_version,
            "true" if config.install_requirements else "false",
        ]
    )
    run([prefix / "bin/python3", "-I", "-m", "pip", "check"])
    for name in ("pip", "pip3"):
        run([prefix / "bin" / name, "--version"])
    if config.install_requirements:
        run([prefix / "bin/wheel", "version"])


def remove_output(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def assign_output_owner(prefix):
    # A build elevated by sudo still publishes an install owned by its caller.
    if os.geteuid() != 0 or "SUDO_UID" not in os.environ:
        return
    uid, gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    os.chown(prefix, uid, gid, follow_symlinks=False)
    for directory, dirs, files in os.walk(prefix):
        for name in dirs + files:
            os.chown(Path(directory) / name, uid, gid, follow_symlinks=False)


def publish_install(config, scratch):
    config.output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=".cpython-standalone.", dir=config.output.parent)
    )
    staged, previous = stage / "install", stage / "previous"
    replacing = False
    retain_backup = False
    try:
        # Move across filesystems before touching the previous output. Removing
        # the original install path also exposes absolute console-script shebangs.
        shutil.move(scratch / "python/install", staged)
        verify_install(config, config.source, staged)
        (staged / ".standalone-build-output").touch()
        assign_output_owner(staged)
        if config.output.exists():
            config.output.rename(previous)
        replacing = True
        staged.rename(config.output)
        verify_install(config, config.source, config.output)
    except BaseException:
        with ignore_interrupts():
            if previous.exists():
                log(f"Restoring previous output: {config.output}")
                try:
                    remove_output(config.output)
                    previous.rename(config.output)
                except OSError as error:
                    retain_backup = True
                    raise BuildError(
                        f"Restore failed; previous output remains in {previous}: {error}"
                    ) from error
            elif replacing:
                remove_output(config.output)
        raise
    finally:
        if not retain_backup:
            with ignore_interrupts():
                shutil.rmtree(stage)


def run_build(config, scratch):
    log(
        f"3/6 Build CPython {config.python_version} ({config.target}, {config.options})"
    )
    build_distribution(config, scratch)
    extract_distribution(config, scratch)
    log("4/6 Run PBS distribution tests")
    verify_distribution(config, scratch)
    if config.install_requirements:
        log("5/6 Install requirements and make console scripts relocatable")
        install_requirements(config, scratch)
    else:
        log("5/6 Skip optional requirements installation")
    log("6/6 Verify and publish the uncompressed install tree")
    publish_install(config, scratch)
    log(f"Standalone install tree: {config.output}")


def check_docker_sdk(config):
    # PBS uses docker-py as well as the CLI. Test its actual uv environment.
    run(
        [
            "uv",
            "run",
            "--no-dev",
            "python",
            "-c",
            "import docker; docker.from_env(timeout=10).ping()",
        ],
        cwd=config.builder,
    )


def on_signal(signum, frame):
    raise SystemExit(128 + signum)


def main():
    if sys.argv[1:] and sys.argv[1] in {"--help", "-h"}:
        print(USAGE)
        return 0
    if len(sys.argv) != 1:
        print(
            "Error: Unexpected arguments. Use --help for environment variables.",
            file=sys.stderr,
        )
        return 1
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    try:
        config = Config.from_environment()
        check_paths(config)
        log("1/6 Prepare the python-build-standalone checkout")
        with workspace(config) as scratch:
            log("2/6 Check tools and connect Docker and Buildx")
            check_environment(config)
            with ensure_docker(config) as config:
                check_docker_sdk(config)
                run_build(config, scratch)
    except (DockerPermissionError, PermissionError) as error:
        if os.geteuid() == 0 or shutil.which("sudo") is None:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        # Release workspace locks before restarting the whole process. Keep SDK,
        # Buildx, proxies and build settings in one environment, without an outer container.
        log(f"{error} Retry the build with sudo.")
        os.execvp(
            "sudo",
            [
                "sudo",
                "-n",
                "-E",
                "env",
                f"PATH={os.environ['PATH']}",
                f"DOCKER_CONFIG={os.environ.get('DOCKER_CONFIG') or Path.home() / '.docker'}",
                sys.executable,
                str(Path(__file__).resolve()),
            ],
        )
    except (BuildError, DockerError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"Error: {error}", file=sys.stderr)
        return error.returncode if error.returncode > 0 else 128 - error.returncode
    return 0


USAGE = Path(__file__).with_name("usage.txt").read_text()


if __name__ == "__main__":
    sys.exit(main())
