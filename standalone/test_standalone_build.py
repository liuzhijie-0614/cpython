"""Host-side regression tests: python3 standalone/test_standalone_build.py."""

import builtins
import fcntl
import json
import os
import signal
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
BUILD_DIR = Path(__file__).resolve().parent
ROOT = BUILD_DIR.parent
sys.path.insert(0, str(BUILD_DIR))

import build_standalone as build
import standalone_docker as docker
import verify_standalone_install as verify


class StandaloneBuildTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="test-standalone.")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = build.Config(
            self.root / "source",
            self.root / "workspace",
            "release",
            self.root / "output",
            "pgo+lto",
            "x86_64-unknown-linux-gnu",
            "3.14.10",
            "test",
            "1.0.0.5",
        )
        self.config.work_dir.mkdir()
        self.scratch = self.root / "scratch"
        self.install = self.scratch / "python/install"
        self.install.mkdir(parents=True)
        (self.install / "new").write_text("new output")

    def previous_output(self):
        self.config.output.mkdir()
        (self.config.output / "old").write_text("previous output")
        (self.config.output / ".standalone-build-output").touch()

    def assert_restored(self):
        self.assertEqual(
            (self.config.output / "old").read_text(), "previous output"
        )
        self.assertFalse((self.config.output / "new").exists())
        self.assertEqual(list(self.root.glob(".cpython-standalone.*")), [])

    def test_compiler_combinations_and_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                build.Config.from_environment().options, "pgo+lto"
            )
            for prefix in ("", "freethreaded+", "free-threaded+"):
                for option in ("noopt", "debug", "pgo", "lto", "pgo+lto"):
                    with self.subTest(prefix=prefix, option=option):
                        os.environ["CUSTOM_COMPILER_OPTIONS"] = prefix + option
                        actual = build.Config.from_environment().options
                        self.assertEqual(
                            actual,
                            prefix.replace("free-threaded", "freethreaded")
                            + option,
                        )
            for invalid in (
                "freethreaded+gpo+lto",
                "noopt+lto",
                "freethreaded+freethreaded+pgo",
            ):
                os.environ["CUSTOM_COMPILER_OPTIONS"] = invalid
                with self.assertRaises(build.BuildError):
                    build.Config.from_environment()

    def test_install_verification_accepts_matching_git_metadata(self):
        for source_identifier, identifier, revision in (
            ("heads/branch", "heads/branch", "deadbeef"),
            ("heads/branch", "heads/branch-dirty", "deadbeef12345678"),
            ("tags/v3.14.10", "tags/v3.14.10-dirty", "deadbeef"),
            ("deadbeef", "deadbeef-dirty", "deadbeef"),
            ("heads/branch-dirty", "heads/branch-dirty", "deadbeef"),
            ("heads/branch-dirty", "heads/branch-dirty-dirty", "deadbeef"),
        ):
            with (
                self.subTest(identifier=identifier, revision=revision),
                self.verification_context(
                    git_info=("CPython", identifier, revision),
                    source_identifier=source_identifier,
                ) as command,
            ):
                (self.config.source / ".git").mkdir(exist_ok=True)
                verify.main()
                git_commands = [
                    call.args[0] for call in command.call_args_list
                    if call.args[0][0] == "git"
                ]
                git_prefix = [
                    "git", "-c", f"safe.directory={self.config.source}",
                    "-C", self.config.source,
                ]
                self.assertEqual(git_commands, [
                    [*git_prefix, "rev-parse", "HEAD"],
                    [*git_prefix, "describe", "--all", "--always"],
                ])

    def test_install_verification_rejects_missing_git_metadata(self):
        for identifier, revision in (("", ""), ("heads/branch", ""), ("", "deadbeef")):
            for git_is_file in (False, True):
                with (
                    self.subTest(identifier=identifier, revision=revision, file=git_is_file),
                    self.verification_context(git_info=("CPython", identifier, revision)),
                    self.assertRaisesRegex(SystemExit, "Git metadata is missing"),
                ):
                    git_path = self.config.source / ".git"
                    if git_is_file:
                        git_path.write_text("gitdir: /other/worktree\n")
                    else:
                        git_path.mkdir()
                    try:
                        verify.main()
                    finally:
                        if git_is_file:
                            git_path.unlink()
                        else:
                            git_path.rmdir()

    def test_source_archive_accepts_missing_git_metadata(self):
        with self.verification_context(git_info=("CPython", "", "")) as command:
            verify.main()
            self.assertTrue(all(
                call.args[0][0] != "git" for call in command.call_args_list
            ))

    def test_install_verification_rejects_wrong_git_metadata(self):
        for identifier, revision, error in (
            ("heads/branch", "badbeef", "Git revision"),
            ("heads/other", "deadbeef", "Git identifier"),
            ("heads/other-dirty", "deadbeef", "Git identifier"),
        ):
            with (
                self.subTest(identifier=identifier, revision=revision),
                self.verification_context(git_info=("CPython", identifier, revision)),
                self.assertRaisesRegex(SystemExit, error),
            ):
                (self.config.source / ".git").mkdir(exist_ok=True)
                verify.main()

    def container_fixture(self):
        # Only shell utilities and a fake Docker are on PATH: no Python, uv or Make.
        source = self.config.source
        (source / "standalone").mkdir(parents=True)
        shutil.copy2(ROOT / "build_standalone.sh", source / "build_standalone.sh")
        for name in ("container_build.sh", "usage.txt"):
            shutil.copy2(BUILD_DIR / name, source / "standalone" / name)
        tools = self.root / "shell-bin"
        tools.mkdir()
        for name in ("bash", "dirname", "realpath", "mkdir", "cat", "id"):
            (tools / name).symlink_to(shutil.which(name))
        home = self.root / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".docker").mkdir()
        (home / ".docker/config.json").write_text('{"auths":{"registry.example":{}}}')
        (tools / "getent").write_text(
            '#!/bin/bash\nprintf "caller:x:1001:1002::%s:/bin/bash\\n" "$STANDALONE_TEST_HOME"\n'
        )
        (tools / "getent").chmod(0o755)
        (tools / "docker").write_text(
            '#!/bin/bash\noriginal=("$@")\nshift 2\ncase $1 in\n'
            'context) printf "%s\\n" "$STANDALONE_TEST_ENDPOINT" ;;\n'
            'info) exit 0 ;;\n'
            'run) printf "%s\\0" "${original[@]}" > "$STANDALONE_TEST_ARGS"; '
            'exit "${STANDALONE_TEST_EXIT:-0}" ;;\nesac\n'
        )
        (tools / "docker").chmod(0o755)
        endpoint = self.root / "docker.sock"
        endpoint.touch()
        return {
            "PATH": str(tools), "HOME": str(home),
            "CUSTOM_STANDALONE_BUILD_IMAGE": "true",
            "CUSTOM_STANDALONE_WORK_DIR": str(self.config.work_dir),
            "STANDALONE_OUTPUT_DIR": str(self.config.output),
            "SUDO_UID": "1001", "SUDO_GID": "1002",
            "DOCKER_HOST": "unix://" + str(endpoint),
            "STANDALONE_TEST_ENDPOINT": "unix://" + str(endpoint),
            "STANDALONE_TEST_ARGS": str(self.root / "container-args"),
            "STANDALONE_TEST_HOME": str(home),
        }

    def run_container_entry(self, env, *args):
        return subprocess.run(
            [str(self.config.source / "build_standalone.sh"), *args],
            cwd=self.root, env=env, capture_output=True, text=True,
        )

    def container_args(self):
        return (self.root / "container-args").read_bytes().decode().rstrip("\0").split("\0")

    def test_container_entry_runs_without_host_python(self):
        env = self.container_fixture()
        self.assertIsNone(shutil.which("python3", path=env["PATH"]))
        result = self.run_container_entry(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.container_args()
        self.assertEqual(args[args.index("--workdir") + 1], str(self.config.source))
        self.assertEqual(args[-2:], ["python3", str(self.config.source / "standalone/build_standalone.py")])
        self.assertIn("--privileged", args)
        self.assertIn("--network=host", args)
        self.assertIn("--rm", args)

    def test_container_defaults_use_hidden_workspace_and_mount_parents_once(self):
        env = self.container_fixture()
        env.pop("CUSTOM_STANDALONE_WORK_DIR")
        env.pop("STANDALONE_OUTPUT_DIR")
        result = self.run_container_entry(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.container_args()
        forwarded = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--env")
        self.assertEqual(forwarded["CUSTOM_STANDALONE_WORK_DIR"], str(self.config.source.parent / ".cpython-standalone"))
        self.assertEqual(forwarded["STANDALONE_OUTPUT_DIR"], str(self.config.source / "output"))
        mounts = [args[i + 1] for i, value in enumerate(args) if value == "--mount"]
        self.assertEqual(len(mounts), len(set(mounts)))

    def test_native_entry_requires_python_and_does_not_launch_container(self):
        env = self.container_fixture()
        env["CUSTOM_STANDALONE_BUILD_IMAGE"] = "false"
        result = self.run_container_entry(env)
        self.assertEqual(result.returncode, 127)
        self.assertIn("python3", result.stderr)
        self.assertFalse((self.root / "container-args").exists())

    def test_container_preserves_paths_options_and_readonly_credentials(self):
        env = self.container_fixture()
        home = Path(env["HOME"])
        settings = home / ".docker/config.json"
        original = settings.read_text()
        config = replace(self.config, work_dir=self.root / "cache", output=self.root / "results/python")
        env.update(
            CUSTOM_STANDALONE_WORK_DIR=str(config.work_dir),
            STANDALONE_OUTPUT_DIR=str(config.output),
            CUSTOM_STANDALONE_BUILD_BRANCH="feature/example",
            CUSTOM_COMPILER_OPTIONS="freethreaded+pgo+lto",
            CUSTOM_INSTALL_REQUIREMENTS="true", BUILD_VERSION="1.0.0.5",
            PYBUILD_RELEASE_TAG="test", http_proxy="",
            UV_INDEX_URL="https://index.example/simple",
            UV_PROJECT_ENVIRONMENT=str(home / "host-venv"),
            DOCKER_CONTEXT="selected", DOCKER_HOST="tcp://ignored:2375",
        )
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(home / "agent.sock"))
            env["SSH_AUTH_SOCK"] = str(home / "agent.sock")
            result = self.run_container_entry(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.container_args()
        mounts = [args[i + 1] for i, value in enumerate(args) if value == "--mount"]
        for path in (config.source.parent, config.work_dir.parent, config.output.parent):
            self.assertIn(f"type=bind,src={path},dst={path}", mounts)
        self.assertIn(f"type=bind,src={home / '.docker'},dst=/run/standalone-docker-source,readonly", mounts)
        self.assertIn(f"type=bind,src={home / '.ssh'},dst=/run/standalone-ssh,readonly", mounts)
        forwarded = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--env")
        for key in ("CUSTOM_STANDALONE_WORK_DIR", "STANDALONE_OUTPUT_DIR", "CUSTOM_STANDALONE_BUILD_BRANCH", "CUSTOM_COMPILER_OPTIONS", "CUSTOM_INSTALL_REQUIREMENTS", "BUILD_VERSION", "PYBUILD_RELEASE_TAG", "SUDO_UID", "SUDO_GID", "http_proxy", "UV_INDEX_URL"):
            self.assertEqual(forwarded[key], env[key])
        self.assertNotIn("CUSTOM_STANDALONE_BUILD_IMAGE", forwarded)
        self.assertEqual(forwarded["UV_PROJECT_ENVIRONMENT"], "/run/standalone/pbs-venv")
        self.assertEqual(sum(
            args[i + 1].startswith("UV_PROJECT_ENVIRONMENT=")
            for i, value in enumerate(args) if value == "--env"
        ), 1)
        self.assertEqual(forwarded["DOCKER_HOST"], "unix:///var/run/docker.sock")
        self.assertEqual(forwarded["SSH_AUTH_SOCK"], "/run/standalone-ssh-agent")
        self.assertNotIn("DOCKER_CONTEXT", forwarded)
        self.assertEqual(settings.read_text(), original)

    def test_container_failure_preserves_exit_code_and_previous_output(self):
        env = self.container_fixture()
        self.previous_output()
        env["STANDALONE_TEST_EXIT"] = "37"
        env["UV_INDEX_URL"] = "https://private-test-token@index.example"
        result = self.run_container_entry(env)
        self.assertEqual(result.returncode, 37)
        self.assertNotIn("private-test-token", result.stdout + result.stderr)
        self.assert_restored()

    def test_container_requires_local_docker_for_bind_mounts(self):
        env = self.container_fixture()
        env["DOCKER_HOST"] = "tcp://remote:2375"
        result = self.run_container_entry(env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("local Docker", result.stderr)
        self.assertFalse((self.root / "container-args").exists())

    def test_shell_help_and_invalid_arguments_need_neither_python_nor_docker(self):
        env = self.container_fixture()
        (Path(env["PATH"]) / "docker").unlink()
        for flag in ("--help", "-h"):
            result = self.run_container_entry(env, flag)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, build.USAGE)
        result = self.run_container_entry(env, "unexpected")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unexpected arguments", result.stderr)

    def test_output_path_guards(self):
        unsafe = (
            Path("/"),
            self.root,
            self.config.source,
            self.config.builder,
            self.config.builder / "install",
            self.config.work,
            self.config.work.parent,
        )
        with (
            patch.object(build, "run"),
            patch.object(build.shutil, "which", return_value="tool"),
        ):
            for output in unsafe:
                with (
                    self.subTest(output=output),
                    self.assertRaises(build.BuildError),
                ):
                    build.check_paths(replace(self.config, output=output))
            self.config.output.mkdir()
            with self.assertRaises(build.BuildError):
                build.check_paths(self.config)
            (self.config.output / ".standalone-build-output").touch()
            build.check_paths(self.config)
            custom = replace(
                self.config, output=self.config.source / "artifacts/python"
            )
            build.check_paths(custom)
            custom.output.mkdir(parents=True)
            with self.assertRaises(build.BuildError):
                build.check_paths(custom)
            (custom.output / ".standalone-build-output").touch()
            build.check_paths(custom)

    def test_install_requirements_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(
                build.Config.from_environment().install_requirements
            )
            for value, enabled in (
                ("", False), ("false", False), ("true", True), ("ture", False)
            ):
                with self.subTest(value=value):
                    os.environ["CUSTOM_INSTALL_REQUIREMENTS"] = value
                    self.assertEqual(
                        build.Config.from_environment().install_requirements,
                        enabled,
                    )

    def test_requirements_installation_only_runs_when_enabled(self):
        for enabled in (False, True):
            with (
                self.subTest(enabled=enabled),
                patch.object(build, "build_distribution"),
                patch.object(build, "extract_distribution"),
                patch.object(build, "verify_distribution"),
                patch.object(build, "publish_install") as publish,
                patch.object(build, "install_requirements") as install,
            ):
                config = replace(self.config, install_requirements=enabled)
                build.run_build(config, self.scratch)
                if enabled:
                    install.assert_called_once_with(config, self.scratch)
                else:
                    install.assert_not_called()
                publish.assert_called_once_with(config, self.scratch)

    def test_install_verification_only_requires_wheel_when_enabled(self):
        for enabled in (False, True):
            with (
                self.subTest(enabled=enabled),
                patch.object(build, "run") as run,
            ):
                config = replace(self.config, install_requirements=enabled)
                build.verify_install(config, config.source, self.install)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(
                    commands[0][-3:],
                    [
                        config.source, config.build_version,
                        "true" if enabled else "false",
                    ],
                )
                self.assertIn(
                    [self.install / "bin/python3", "-I", "-m", "pip", "check"],
                    commands,
                )
                self.assertEqual(
                    [self.install / "bin/wheel", "version"] in commands,
                    enabled,
                )

    @contextmanager
    def verification_context(
        self, enabled=False, incorrect=None,
        git_info=("CPython", "heads/branch-dirty", "deadbeef"),
        source_identifier="heads/branch",
    ):
        prefix = self.install.resolve()
        source = self.config.source
        (source / "Include").mkdir(parents=True, exist_ok=True)
        (source / "Include/pybyted.h").write_text(
            '#define BM_SEPARATOR "bm"\n'
            "#define PY_MAJOR_BYTEDVERSION 1\n"
            "#define PY_MINOR_BYTEDVERSION 0\n"
        )

        def output(args, **kwargs):
            if args[0] == "git":
                return (
                    "deadbeef12345678\n" if "rev-parse" in args
                    else source_identifier + "\n"
                )
            name, flag = Path(args[0]).name, args[-1]
            version = "3.14.10.bm.1.0.5" if flag == "--BV" else "3.14.10"
            if flag == "-VV":
                version = sys.version
            if (name, flag) == incorrect:
                version = (
                    "3.14.10 (heads/other:badbeef, test) [Clang]"
                    if flag == "-VV" else "0.0.0"
                )
            return f"Python {version}\n"

        distribution = SimpleNamespace(
            version="1.0", locate_file=lambda path: prefix / "lib"
        )
        with (
            patch.object(
                sys, "argv", [
                    "verify_standalone_install.py", str(prefix), "3.14.10",
                    "pgo+lto", str(source), "1.0.0.5",
                    "true" if enabled else "false",
                ],
            ),
            patch.object(
                sys, "version",
                f"3.14.10 ({git_info[1]}:{git_info[2]}, test) [Clang]",
            ),
            patch.object(sys, "_git", git_info),
            patch.object(sys, "prefix", str(prefix)),
            patch.object(sys, "base_prefix", str(prefix)),
            patch.object(sys, "executable", str(prefix / "bin/python3")),
            patch.object(verify.sysconfig, "get_config_var", return_value=0),
            patch.object(
                verify.metadata, "distribution", return_value=distribution
            ),
            patch.object(
                verify.subprocess, "check_output", side_effect=output
            ) as command,
        ):
            yield command

    def test_default_verifier_needs_no_requirements_or_packaging(self):
        original_import = builtins.__import__

        def without_packaging(name, *args, **kwargs):
            if name == "packaging" or name.startswith("packaging."):
                raise ModuleNotFoundError("packaging is not installed")
            return original_import(name, *args, **kwargs)

        with (
            self.verification_context() as command,
            patch.object(builtins, "__import__", side_effect=without_packaging),
        ):
            verify.main()
        self.assertEqual(command.call_count, 6)
        self.assertFalse((self.config.source / "requirements.txt").exists())

    def test_verifier_checks_requirements_when_enabled(self):
        with (
            self.verification_context(enabled=True),
            patch.object(verify, "check_requirements") as requirements,
        ):
            verify.main()
            requirements.assert_called_once_with(
                self.install.resolve(), self.config.source
            )

    def test_verifier_rejects_wrong_normal_and_byted_versions_for_both_aliases(self):
        for name in ("python3", "python"):
            for flag in ("--version", "--BV"):
                with (
                    self.subTest(name=name, flag=flag),
                    self.verification_context(incorrect=(name, flag)),
                    self.assertRaisesRegex(SystemExit, f"{name} {flag}: expected"),
                ):
                    verify.main()

    def test_verifier_rejects_wrong_verbose_build_info_for_both_aliases(self):
        for name in ("python3", "python"):
            with (
                self.subTest(name=name),
                self.verification_context(incorrect=(name, "-VV")),
                self.assertRaisesRegex(SystemExit, f"{name} -VV: expected"),
            ):
                (self.config.source / ".git").mkdir(exist_ok=True)
                verify.main()

    def test_work_directory_defaults_and_custom_locations(self):
        with patch.dict(os.environ, {}, clear=True):
            config = build.Config.from_environment()
            self.assertEqual(
                config.builder,
                config.source.parent / ".cpython-standalone/python-build-standalone",
            )
            self.assertEqual(config.work, config.source.parent / ".cpython-standalone/work")
            for directory in (
                config.source.parent / "pbs-cache",
                self.root / "custom-builder",
            ):
                with self.subTest(directory=directory):
                    os.environ["CUSTOM_STANDALONE_WORK_DIR"] = str(directory)
                    custom = build.Config.from_environment()
                    self.assertEqual(custom.work_dir, directory)
                    self.assertEqual(custom.builder, directory / "python-build-standalone")
                    self.assertEqual(custom.work, directory / "work")
                    build.check_paths(custom)
            for directory in (
                config.source,
                config.source.parent,
                config.source / "pbs-cache",
            ):
                with self.subTest(directory=directory):
                    with self.assertRaises(build.BuildError):
                        build.check_paths(replace(config, work_dir=directory))

    def test_publish_success_after_both_checks(self):
        self.previous_output()
        checked = []

        def verify(config, scratch, prefix):
            checked.append(prefix)
            self.assertFalse(self.install.exists())
            self.assertTrue((prefix / "new").is_file())
            if len(checked) == 1:
                self.assertTrue((config.output / "old").is_file())

        with patch.object(build, "verify_install", side_effect=verify):
            build.publish_install(self.config, self.scratch)
        self.assertEqual(len(checked), 2)
        self.assertEqual(checked[1], self.config.output)
        self.assertFalse((self.config.output / "old").exists())
        self.assertTrue(
            (self.config.output / ".standalone-build-output").is_file()
        )
        self.assertEqual(list(self.root.glob(".cpython-standalone.*")), [])

    def test_failed_staging_check_keeps_previous_output(self):
        self.previous_output()
        with patch.object(
            build, "verify_install", side_effect=build.BuildError("staging")
        ):
            with self.assertRaises(build.BuildError):
                build.publish_install(self.config, self.scratch)
        self.assert_restored()

    def test_failed_final_check_restores_previous_output(self):
        self.previous_output()
        with patch.object(
            build,
            "verify_install",
            side_effect=[None, build.BuildError("final")],
        ):
            with self.assertRaises(build.BuildError):
                build.publish_install(self.config, self.scratch)
        self.assert_restored()

    def test_failed_first_publication_removes_output(self):
        with patch.object(
            build,
            "verify_install",
            side_effect=[None, build.BuildError("final")],
        ):
            with self.assertRaises(build.BuildError):
                build.publish_install(self.config, self.scratch)
        self.assertFalse(self.config.output.exists())
        self.assertEqual(list(self.root.glob(".cpython-standalone.*")), [])

    def test_sigterm_during_final_check_restores_previous_output(self):
        self.previous_output()
        original_handler = signal.signal(signal.SIGTERM, build.on_signal)
        self.addCleanup(signal.signal, signal.SIGTERM, original_handler)

        def verify(config, scratch, prefix):
            if prefix == config.output:
                os.kill(os.getpid(), signal.SIGTERM)

        with patch.object(build, "verify_install", side_effect=verify):
            with self.assertRaises(SystemExit) as caught:
                build.publish_install(self.config, self.scratch)
        self.assertEqual(caught.exception.code, 143)
        self.assert_restored()

    def test_interruption_after_previous_output_move_restores_it(self):
        self.previous_output()
        rename = Path.rename

        def interrupted_rename(path, destination):
            result = rename(path, destination)
            if Path(destination).name == "previous":
                raise SystemExit(130)
            return result

        with (
            patch.object(build, "verify_install"),
            patch.object(Path, "rename", interrupted_rename),
        ):
            with self.assertRaises(SystemExit):
                build.publish_install(self.config, self.scratch)
        self.assert_restored()

    def test_failed_restore_retains_backup(self):
        self.previous_output()
        rename = Path.rename

        def failing_restore(path, destination):
            if path.name == "previous":
                raise OSError("cannot restore")
            return rename(path, destination)

        with patch.object(
            build,
            "verify_install",
            side_effect=[None, build.BuildError("final")],
        ):
            with patch.object(Path, "rename", failing_restore):
                with self.assertRaisesRegex(
                    build.BuildError, "previous output remains"
                ):
                    build.publish_install(self.config, self.scratch)
        backups = list(self.root.glob(".cpython-standalone.*/previous/old"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "previous output")

    def test_workspace_locks_match_flock_and_cleanup_on_failure(self):
        with patch.object(build, "prepare_builder"):
            with self.assertRaisesRegex(build.BuildError, "build failed"):
                with build.workspace(self.config) as scratch:
                    self.assertEqual(scratch.parent, self.config.work)
                    self.assertFalse(self.config.source.exists())
                    self.assertFalse((scratch / "source").exists())
                    for path in (
                        Path(f"{self.config.builder}.lock"),
                        self.config.builder / "build/.cpython-standalone.lock",
                    ):
                        result = subprocess.run(
                            ["flock", "-n", str(path), "true"], check=False
                        )
                        self.assertEqual(result.returncode, 1)
                    raise build.BuildError("build failed")
        self.assertFalse(scratch.exists())
        result = subprocess.run(
            ["flock", "-n", f"{self.config.builder}.lock", "true"], check=False
        )
        self.assertEqual(result.returncode, 0)

    def test_existing_lock_prevents_clone(self):
        with Path(f"{self.config.builder}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(build, "prepare_builder") as prepare:
                with self.assertRaisesRegex(
                    build.BuildError, "Another standalone build"
                ):
                    with build.workspace(self.config):
                        self.fail("locked workspace entered")
                prepare.assert_not_called()

    def test_config_source_is_independent_of_current_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, {}, clear=True):
                config = build.Config.from_environment()
            self.assertEqual(config.source, ROOT)
        finally:
            os.chdir(previous)

    def test_build_forwards_options_version_and_rebuild_flags(self):
        utils = self.config.builder / "pythonbuild/utils.py"
        utils.parent.mkdir(parents=True)
        original = "# The builder checkout must remain unchanged.\n"
        utils.write_text(original)
        with patch.object(build, "run") as run:
            build.build_distribution(self.config, self.scratch)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--python-source") + 1], self.config.source)
        self.assertEqual(args[args.index("--options") + 1], "pgo+lto")
        self.assertEqual(args[args.index("--build-version") + 1], "1.0.0.5")
        env = run.call_args.kwargs["env"]
        self.assertEqual(utils.read_text(), original)
        self.assertNotIn("PYBUILD_SOURCE_EXCLUDES", env)
        self.assertEqual(env["PYBUILD_PYTHON_VERSION"], "3.14.10")
        for name in ("build-cpython.sh", "build-cpython-host.sh"):
            self.assertIn(
                f"--assume-new={self.config.builder}/cpython-unix/{name}",
                env["MAKEFLAGS"],
            )
        with patch.object(build, "run") as run:
            build.verify_distribution(self.config, self.scratch)
        self.assertNotIn("TERM", run.call_args.kwargs["env"])

    def test_image_network_is_managed_by_the_driver(self):
        with patch.object(build, "run") as run:
            build.build_distribution(self.config, self.scratch)
        env = run.call_args.kwargs["env"]
        env["STANDALONE_DOCKER_CLI"] = str(self.root / "docker-cli")
        Path(env["STANDALONE_DOCKER_CLI"]).write_text(
            '#!/usr/bin/env python3\nimport sys\nprint("\\n".join(sys.argv[1:]))\n'
        )
        Path(env["STANDALONE_DOCKER_CLI"]).chmod(0o755)
        wrapper = self.scratch / "docker-bin/docker"
        for args, expected in (
            (
                ["buildx", "build", "."],
                ["buildx", "build", "--network=host", "."],
            ),
            (["info"], ["info"]),
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [str(wrapper), *args], env=env, capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)

    def test_entrypoints_help_and_invalid_arguments_without_docker(self):
        for entry in (
            [sys.executable, str(BUILD_DIR / "build_standalone.py")],
            [str(ROOT / "build_standalone.sh")],
        ):
            with self.subTest(entry=entry):
                for flag in ("--help", "-h"):
                    result = subprocess.run(
                        [*entry, flag],
                        cwd=self.root,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("CUSTOM_COMPILER_OPTIONS", result.stdout)
                    self.assertIn("standalone/README.md", result.stdout)
                    for internal in (
                        "DOCKER_HOST", "DOCKER_CONTEXT", "BUILDX_BUILDER"
                    ):
                        self.assertNotIn(internal, result.stdout)
                result = subprocess.run(
                    [*entry, "unexpected"],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("Unexpected arguments", result.stderr)

    def wait_for_file(self, path, process):
        deadline = time.monotonic() + 5
        while not path.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail(f"Process did not create {path}")
            time.sleep(0.01)

    def stop_process(self, process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def test_sigterm_stops_subprocess_descendants_and_preserves_status(self):
        ready, stopped = self.root / "ready", self.root / "stopped"
        grandchild = (
            "import signal, sys\nfrom pathlib import Path\n"
            f"def stop(sig, frame):\n    Path({str(stopped)!r}).touch()\n    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            f"Path({str(ready)!r}).touch()\nsignal.pause()\n"
        )
        child = f"import subprocess, sys\nsubprocess.run([sys.executable, '-c', {grandchild!r}])\n"
        driver = (
            "import sys\nfrom pathlib import Path\nfrom dataclasses import replace\nfrom contextlib import nullcontext\n"
            f"sys.path.insert(0, {str(BUILD_DIR)!r})\nimport build_standalone as build\n"
            "build.prepare_builder = lambda config: None\n"
            "build.check_paths = lambda config: None\n"
            "build.check_docker_sdk = lambda config: None\n"
            "build.check_environment = lambda config: None\n"
            "original_config = build.Config.from_environment\n"
            f"build.Config.from_environment = lambda: replace(original_config(), work_dir=Path({str(self.config.work_dir)!r}))\n"
            "build.ensure_docker = nullcontext\n"
            f"build.run_build = lambda config, scratch: build.run([sys.executable, '-c', {child!r}])\n"
            "sys.exit(build.main())\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", driver], stdout=subprocess.PIPE
        )
        self.addCleanup(self.stop_process, process)
        self.wait_for_file(ready, process)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143)
        self.assertTrue(stopped.exists())

    def docker_fixture(self, reuse):
        # Run the Python lifecycle with fake Docker commands. Patch system paths
        # and the root check so all tests work without privileges or a daemon.
        tools = self.root / "bin"
        tools.mkdir()
        cli = tools / "docker"
        cli.write_text(
            '#!/usr/bin/env bash\ncase "$1" in\n'
            "info) [[ ${STANDALONE_TEST_INFO_FAIL:-0} != 1 ]] && "
            "[[ ${STANDALONE_TEST_REUSE} == 1 || -f ${STANDALONE_TEST_ROOT}/daemon-ready ]] ;;\n"
            "buildx)\n"
            "  [[ ${STANDALONE_TEST_BUILDX_FAIL:-0} != 1 ]] || exit 1\n"
            '  echo "$*" >> "${STANDALONE_TEST_ROOT}/buildx-calls"\n'
            "  if [[ ${STANDALONE_TEST_DEFAULT_BUILDER_FAIL:-0} == 1 && $2 == inspect ]]; then\n"
            "    [[ $3 != default && -f ${STANDALONE_TEST_ROOT}/builder-created ]] || exit 1\n"
            "  fi\n"
            '  [[ $2 != create ]] || touch "${STANDALONE_TEST_ROOT}/builder-created" ;;\n'
            'context) printf \'{"Endpoints":{"docker":{"Host":"%s"}},"TLSMaterial":{}}\\n\' '
            '"${STANDALONE_TEST_ENDPOINT:-unix://${STANDALONE_TEST_ROOT}/docker.sock}" ;;\n'
            "*) exit 1 ;;\nesac\n"
        )
        cli.chmod(0o755)
        daemon = tools / "dockerd"
        daemon.write_text(
            f"#!{sys.executable}\nimport os, signal, sys\nfrom pathlib import Path\n"
            "root = Path(os.environ['STANDALONE_TEST_ROOT'])\n"
            "if os.environ.get('STANDALONE_TEST_DAEMON_FAIL'):\n"
            "    print('fake daemon startup failure', flush=True)\n    sys.exit(23)\n"
            "def stop(sig, frame):\n"
            "    with (root / 'events').open('a') as stream:\n"
            "        stream.write('daemon stopped\\n')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "(root / 'daemon-ready').touch()\nsignal.pause()\n"
        )
        daemon.chmod(0o755)
        env = os.environ.copy()
        for variable in (
            "DOCKER_CONTEXT",
            "STANDALONE_DOCKER_CONFIG_SOURCE",
            "STANDALONE_TEST_ENDPOINT",
            "STANDALONE_TEST_INFO_FAIL",
            "STANDALONE_TEST_BUILDX_FAIL",
            "STANDALONE_TEST_DEFAULT_BUILDER_FAIL",
            "BUILDX_BUILDER",
            "STANDALONE_TEST_DAEMON_FAIL",
        ):
            env.pop(variable, None)
        env.update(
            PATH=f"{tools}:{env['PATH']}",
            STANDALONE_TEST_ROOT=str(self.root),
            STANDALONE_TEST_REUSE=str(int(reuse)),
            DOCKER_HOST=f"unix://{self.root}/docker.sock",
            DOCKER_CONFIG=str(self.root / "client"),
            http_proxy="",
            https_proxy="",
            no_proxy="*",
        )
        for name, value in (
            ("DATA_ROOT", self.root / "data"),
            ("EXEC_ROOT", self.root / "run"),
            ("LOG_PATH", self.root / "daemon.log"),
        ):
            mock = patch.object(docker, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        return env

    def docker_driver(self, body):
        # Patch only external resources; retain main(), ensure_docker(), signal
        # handling, publication and subprocess shutdown in the child process.
        return (
            "import os, sys\nfrom pathlib import Path\nfrom dataclasses import replace\n"
            f"sys.path.insert(0, {str(BUILD_DIR)!r})\nimport build_standalone as build\n"
            "import standalone_docker as docker\n"
            "build.prepare_builder = lambda config: None\n"
            "build.check_paths = lambda config: None\n"
            "build.check_docker_sdk = lambda config: None\n"
            "root = Path(os.environ['STANDALONE_TEST_ROOT'])\n"
            "docker.DATA_ROOT = root / 'data'\ndocker.EXEC_ROOT = root / 'run'\n"
            "docker.LOG_PATH = root / 'daemon.log'\ndocker.os.geteuid = lambda: 0\n"
            "build.check_environment = lambda config: None\n"
            "original_config = build.Config.from_environment\n"
            f"build.Config.from_environment = lambda: replace(original_config(), work_dir=Path({str(self.config.work_dir)!r}))\n"
            + body
            + "\nsys.exit(build.main())\n"
        )

    def test_docker_reuses_daemon_and_preserves_command_status(self):
        env = self.docker_fixture(reuse=True)
        driver = self.docker_driver(
            "def run_build(config, scratch):\n"
            "    build.run([sys.executable, '-c', 'raise SystemExit(17)'])\n"
            "build.run_build = run_build\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", driver],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 17)
        self.assertFalse((self.root / "daemon-ready").exists())

    def test_docker_stops_after_build_shutdown_and_actual_rollback(self):
        env = self.docker_fixture(reuse=False)
        self.previous_output()
        ready = self.root / "command-ready"
        child = (
            "import os, signal, sys\nfrom pathlib import Path\n"
            "root = Path(os.environ['STANDALONE_TEST_ROOT'])\n"
            "def stop(sig, frame):\n"
            "    with (root / 'events').open('a') as stream:\n"
            "        stream.write('build stopped\\n')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "(root / 'command-ready').touch()\nsignal.pause()\n"
        )
        driver = self.docker_driver(
            "def verify(config, scratch, prefix):\n"
            "    if prefix == config.output:\n"
            f"        build.run([sys.executable, '-c', {child!r}])\n"
            "build.verify_install = verify\n"
            "def run_build(config, scratch):\n"
            "    config = replace(config, output=root / 'output')\n"
            "    try:\n        build.publish_install(config, root / 'scratch')\n"
            "    finally:\n"
            "        assert (config.output / 'old').exists()\n"
            "        assert not (config.output / 'new').exists()\n"
            "        with (root / 'events').open('a') as stream:\n"
            "            stream.write('rollback complete\\n')\n"
            "build.run_build = run_build\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", driver],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_process, process)
        self.wait_for_file(ready, process)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143)
        self.assert_restored()
        self.assertEqual(
            (self.root / "events").read_text().splitlines(),
            ["build stopped", "rollback complete", "daemon stopped"],
        )

    def test_local_daemon_stops_after_success_and_preserves_endpoint_environment(
        self,
    ):
        env = self.docker_fixture(reuse=False)
        env.update(
            DOCKER_CONTEXT="selected-context",
            DOCKER_HOST="tcp://unselected:2375",
        )
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(docker.os, "geteuid", return_value=0),
        ):
            with docker.ensure_docker(self.config):
                self.assertEqual(
                    os.environ["DOCKER_HOST"],
                    f"unix://{self.root}/docker.sock",
                )
                self.assertNotIn("DOCKER_CONTEXT", os.environ)
            self.assertEqual(os.environ["DOCKER_CONTEXT"], "selected-context")
            self.assertEqual(
                os.environ["DOCKER_HOST"], "tcp://unselected:2375"
            )
        self.assertEqual(
            (self.root / "events").read_text(), "daemon stopped\n"
        )

    def test_missing_docker_and_buildx_fail_explicitly(self):
        env = self.docker_fixture(reuse=True)
        with patch.dict(os.environ, env, clear=True):
            with patch.object(docker.shutil, "which", return_value=None):
                with self.assertRaisesRegex(
                    docker.DockerError, "command 'docker' is missing"
                ):
                    with docker.ensure_docker(self.config):
                        self.fail("Docker must be required")
            os.environ["STANDALONE_TEST_BUILDX_FAIL"] = "1"
            with self.assertRaisesRegex(
                docker.DockerError, "Cannot create Buildx"
            ):
                with docker.ensure_docker(self.config):
                    self.fail("Buildx must be required")

    def test_unreachable_remote_daemon_and_socket_do_not_start_local_daemon(
        self,
    ):
        env = self.docker_fixture(reuse=False)
        with patch.dict(os.environ, env, clear=True):
            os.environ["DOCKER_HOST"] = "tcp://remote:2375"
            with self.assertRaisesRegex(
                docker.DockerError, "daemon at .* is unreachable"
            ):
                with docker.ensure_docker(self.config):
                    self.fail("Remote endpoint must not be replaced")
            os.environ["DOCKER_HOST"] = env["DOCKER_HOST"]
            with socket.socket(socket.AF_UNIX) as existing:
                existing.bind(str(self.root / "docker.sock"))
                access = os.access

                def denied(path, mode):
                    return (
                        False
                        if Path(path) == self.root / "docker.sock"
                        else access(path, mode)
                    )

                with (
                    patch.object(docker.os, "access", side_effect=denied),
                    self.assertRaisesRegex(
                        docker.DockerPermissionError, "No permission"
                    ),
                ):
                    with docker.ensure_docker(self.config):
                        self.fail(
                            "Inaccessible socket requires whole-process elevation"
                        )
        self.assertFalse((self.root / "daemon-ready").exists())

    def test_local_daemon_requires_root_and_dockerd(self):
        env = self.docker_fixture(reuse=False)
        with patch.dict(os.environ, env, clear=True):
            with patch.object(docker.os, "geteuid", return_value=1000):
                with self.assertRaisesRegex(
                    docker.DockerError, "requires root"
                ):
                    with docker.ensure_docker(self.config):
                        self.fail("Local startup requires root")
            which = docker.shutil.which
            with (
                patch.object(docker.os, "geteuid", return_value=0),
                patch.object(
                    docker.shutil,
                    "which",
                    side_effect=lambda tool: (
                        None if tool == "dockerd" else which(tool)
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    docker.DockerError, "command 'dockerd' is missing"
                ):
                    with docker.ensure_docker(self.config):
                        self.fail("Local startup requires dockerd")

    def test_daemon_startup_timeout_stops_daemon(self):
        env = self.docker_fixture(reuse=False)
        env["STANDALONE_TEST_INFO_FAIL"] = "1"
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(docker.os, "geteuid", return_value=0),
        ):
            with patch.object(docker, "READY_TIMEOUT", 0.1):
                with self.assertRaisesRegex(
                    docker.DockerError, "did not become ready"
                ):
                    with docker.ensure_docker(self.config):
                        self.fail("Unready daemon must not be used")
        self.assertEqual(
            (self.root / "events").read_text(), "daemon stopped\n"
        )

    def test_failed_daemon_startup_keeps_error_log(self):
        env = self.docker_fixture(reuse=False)
        env["STANDALONE_TEST_DAEMON_FAIL"] = "1"
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(docker.os, "geteuid", return_value=0),
        ):
            with self.assertRaisesRegex(
                docker.DockerError, "did not become ready"
            ):
                with docker.ensure_docker(self.config):
                    self.fail("Failed daemon must not be used")
        self.assertIn(
            "fake daemon startup failure",
            (self.root / "daemon.log").read_text(),
        )

    def test_docker_proxy_config_preserves_credentials_and_mounted_source(
        self,
    ):
        env = self.docker_fixture(reuse=True)
        source = self.root / "mounted-config"
        source.mkdir()
        original = {
            "auths": {"registry": {"auth": "test"}},
            "currentContext": "selected",
        }
        original_path = self.root / "mounted.json"
        original_path.write_text(json.dumps(original))
        (source / "config.json").symlink_to(original_path)
        env["STANDALONE_DOCKER_CONFIG_SOURCE"] = str(source)
        with patch.dict(os.environ, env, clear=True):
            docker.configure_proxy()
        self.assertEqual(json.loads(original_path.read_text()), original)
        destination = Path(env["DOCKER_CONFIG"]) / "config.json"
        config = json.loads(destination.read_text())
        self.assertEqual(config["auths"], original["auths"])
        self.assertEqual(config["currentContext"], "selected")
        self.assertEqual(
            config["proxies"]["default"],
            {"httpProxy": "", "httpsProxy": "", "noProxy": "*"},
        )
        self.assertFalse(destination.is_symlink())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_invalid_client_config_fails_and_absent_proxies_are_supported(
        self,
    ):
        env = self.docker_fixture(reuse=True)
        destination = Path(env["DOCKER_CONFIG"])
        destination.mkdir()
        (destination / "config.json").write_text("invalid json")
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(
                docker.DockerError, "Invalid Docker client configuration"
            ):
                docker.configure_proxy()
            for name in (
                "http_proxy",
                "https_proxy",
                "no_proxy",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
            ):
                os.environ.pop(name, None)
            docker.configure_proxy()

    def test_branch_defaults_to_release_even_with_existing_checkout(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(build.Config.from_environment().branch, "release")
            os.environ["CUSTOM_STANDALONE_BUILD_BRANCH"] = "feature/example"
            self.assertEqual(
                build.Config.from_environment().branch, "feature/example"
            )

    def make_builder_repository(self, path):
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(
            ["git", "-C", str(path), "checkout", "-q", "-b", "release"],
            check=True,
        )
        (path / "cpython-unix").mkdir()
        (path / "build.py").touch()
        (path / "test-distribution.py").touch()
        (path / "cpython-unix/build-main.py").write_text("--build-version")
        (path / "branch.txt").write_text("release")
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        commit = [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
        ]
        subprocess.run(commit + ["Release"], check=True)
        subprocess.run(
            ["git", "-C", str(path), "checkout", "-q", "-b", "feat/example"],
            check=True,
        )
        (path / "branch.txt").write_text("feature")
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(commit + ["Feature"], check=True)

    def test_existing_checkout_switches_to_default_and_explicit_branch(self):
        self.make_builder_repository(self.config.builder)
        with patch.dict(os.environ, {}, clear=True):
            config = replace(
                self.config, branch=build.Config.from_environment().branch
            )
            build.prepare_builder(config)
            self.assertEqual(
                (config.builder / "branch.txt").read_text(), "release"
            )
            os.environ["CUSTOM_STANDALONE_BUILD_BRANCH"] = "feat/example"
            config = replace(
                config, branch=build.Config.from_environment().branch
            )
            build.prepare_builder(config)
            self.assertEqual(
                (config.builder / "branch.txt").read_text(), "feature"
            )

    def test_git_clone_reports_nonempty_destination_and_preserves_files(self):
        self.config.builder.mkdir()
        contents = self.config.builder / "keep"
        contents.write_text("existing data")
        with self.assertRaises(subprocess.CalledProcessError) as error:
            build.prepare_builder(self.config)
        self.assertEqual(error.exception.returncode, 128)
        self.assertEqual(contents.read_text(), "existing data")

    def test_git_fetch_reports_missing_branch_and_preserves_local_edits(self):
        remote = self.root / "remote"
        self.make_builder_repository(remote)
        subprocess.run(
            ["git", "clone", "-q", str(remote), str(self.config.builder)],
            check=True,
        )
        contents = self.config.builder / "branch.txt"
        contents.write_text("local edit")
        with self.assertRaises(subprocess.CalledProcessError):
            build.prepare_builder(replace(self.config, branch="missing"))
        self.assertEqual(contents.read_text(), "local edit")
        branch = subprocess.check_output(
            ["git", "-C", str(self.config.builder), "branch", "--show-current"],
            text=True,
        ).strip()
        self.assertEqual(branch, "feat/example")

    def test_branch_switch_preserves_tracked_edits_and_untracked_cache(self):
        self.make_builder_repository(self.config.builder)
        (self.config.builder / "branch.txt").write_text("local feature edit")
        (self.config.builder / "cache.tar").write_text("cached dependency")
        subprocess.run(
            [
                "git",
                "-C",
                str(self.config.builder),
                "config",
                "user.useConfigOnly",
                "true",
            ],
            check=True,
        )
        with patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        ):
            build.prepare_builder(self.config)
        self.assertEqual(
            (self.config.builder / "branch.txt").read_text(), "release"
        )
        self.assertEqual(
            (self.config.builder / "cache.tar").read_text(),
            "cached dependency",
        )
        saved = subprocess.check_output(
            ["git", "-C", str(self.config.builder), "stash", "show", "-p"],
            text=True,
        )
        self.assertIn("local feature edit", saved)

    def test_single_branch_checkout_fetches_newly_selected_branch(self):
        remote = self.root / "remote"
        self.make_builder_repository(remote)
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--single-branch",
                "--branch",
                "feat/example",
                "--no-local",
                str(remote),
                str(self.config.builder),
            ],
            check=True,
        )
        build.prepare_builder(self.config)
        self.assertEqual(
            (self.config.builder / "branch.txt").read_text(), "release"
        )
        upstream = subprocess.check_output(
            [
                "git",
                "-C",
                str(self.config.builder),
                "rev-parse",
                "--abbrev-ref",
                "@{upstream}",
            ],
            text=True,
        ).strip()
        self.assertEqual(upstream, "origin/release")

    def test_prepare_builder_precedes_tool_and_docker_checks(self):
        events = []
        from contextlib import contextmanager

        @contextmanager
        def connected(config):
            events.append("Docker")
            yield config

        with (
            patch.dict(os.environ, {}),
            patch.object(
                build.Config, "from_environment", return_value=self.config
            ),
            patch.object(
                build,
                "prepare_builder",
                side_effect=lambda config: events.append("PBS"),
            ),
            patch.object(
                build,
                "check_environment",
                side_effect=lambda config: events.append("tools"),
            ),
            patch.object(build, "ensure_docker", connected),
            patch.object(
                build,
                "check_docker_sdk",
                side_effect=lambda config: events.append("SDK"),
            ),
            patch.object(
                build,
                "run_build",
                side_effect=lambda config, scratch: events.append("build"),
            ),
            patch.object(build.os, "execvp") as execute,
            patch.object(sys, "argv", ["build_standalone.py"]),
        ):
            for value in (None, "", "false", "true", "TRUE", "example:builder"):
                with self.subTest(container_option=value):
                    events.clear()
                    if value is None:
                        os.environ.pop("CUSTOM_STANDALONE_BUILD_IMAGE", None)
                    else:
                        os.environ["CUSTOM_STANDALONE_BUILD_IMAGE"] = value
                    self.assertEqual(build.main(), 0)
                    self.assertEqual(events, ["PBS", "tools", "Docker", "SDK", "build"])
                    execute.assert_not_called()

    def test_buildx_creates_missing_builder_at_same_daemon(self):
        env = self.docker_fixture(reuse=True)
        env["STANDALONE_TEST_DEFAULT_BUILDER_FAIL"] = "1"
        env["BUILDX_BUILDER"] = "user-builder"
        with patch.dict(os.environ, env, clear=True):
            with docker.ensure_docker(self.config):
                self.assertEqual(
                    os.environ["BUILDX_BUILDER"], "cpython-standalone"
                )
                self.assertEqual(os.environ["DOCKER_HOST"], env["DOCKER_HOST"])
            self.assertEqual(os.environ["BUILDX_BUILDER"], "user-builder")
        calls = (self.root / "buildx-calls").read_text()
        self.assertIn("buildx create --name cpython-standalone", calls)
        self.assertIn(env["DOCKER_HOST"], calls)
        self.assertIn("buildx inspect cpython-standalone --bootstrap", calls)
        self.assertIn("--driver-opt network=host", calls)
        self.assertIn("--allow-insecure-entitlement network.host", calls)
        self.assertNotIn("user-builder", calls)
        self.assertNotIn("--use", calls)

    def test_context_is_resolved_for_sdk_even_when_daemon_is_reused(self):
        env = self.docker_fixture(reuse=True)
        env.update(
            DOCKER_CONTEXT="selected", DOCKER_HOST="tcp://unselected:2375"
        )
        with patch.dict(os.environ, env, clear=True):
            with docker.ensure_docker(self.config):
                self.assertEqual(
                    os.environ["DOCKER_HOST"],
                    f"unix://{self.root}/docker.sock",
                )
                self.assertNotIn("DOCKER_CONTEXT", os.environ)
            self.assertEqual(
                os.environ["DOCKER_HOST"], "tcp://unselected:2375"
            )
            self.assertEqual(os.environ["DOCKER_CONTEXT"], "selected")

    def test_context_tls_settings_replace_conflicting_environment(self):
        context = {
            "Endpoints": {
                "docker": {
                    "Host": "tcp://selected:2376",
                    "SkipTLSVerify": True,
                }
            },
            "TLSMaterial": {"docker": ["ca.pem", "cert.pem", "key.pem"]},
            "Storage": {"TLSPath": "/tmp/context-tls"},
        }
        with patch.dict(
            os.environ,
            {
                "DOCKER_CONTEXT": "selected",
                "DOCKER_HOST": "tcp://other:2375",
                "DOCKER_CERT_PATH": "/other/certs",
                "DOCKER_TLS_VERIFY": "1",
            },
            clear=True,
        ):
            with patch.object(
                docker.subprocess,
                "check_output",
                return_value=json.dumps(context),
            ):
                self.assertEqual(
                    docker.select_endpoint(), "tcp://selected:2376"
                )
            self.assertEqual(
                os.environ["DOCKER_CERT_PATH"], "/tmp/context-tls/docker"
            )
            self.assertEqual(os.environ["DOCKER_TLS_VERIFY"], "")
            self.assertNotIn("DOCKER_CONTEXT", os.environ)
            context["TLSMaterial"] = {}
            os.environ["DOCKER_CONTEXT"] = "without-tls"
            with patch.object(
                docker.subprocess,
                "check_output",
                return_value=json.dumps(context),
            ):
                docker.select_endpoint()
            self.assertNotIn("DOCKER_CERT_PATH", os.environ)
            self.assertNotIn("DOCKER_TLS_VERIFY", os.environ)


if __name__ == "__main__":
    unittest.main()
