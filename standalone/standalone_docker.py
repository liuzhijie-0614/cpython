"""Connect Docker and Buildx for both development hosts and SCM jobs."""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

DATA_ROOT = Path("/var/lib/docker")
EXEC_ROOT = Path("/var/run/standalone-docker")
LOG_PATH = Path("/var/log/standalone-dockerd.log")
READY_TIMEOUT = 60
SHUTDOWN_TIMEOUT = 15


class DockerError(Exception):
    """A Docker setup failure with a user-facing explanation."""


class DockerPermissionError(DockerError):
    """The complete build needs root to access or start the local daemon."""


@contextmanager
def ignore_interrupts():
    previous = {
        sig: signal.signal(sig, signal.SIG_IGN)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def configure_proxy():
    # Proxies are optional on a development host. Explicit empty values disable them.
    proxies = {}
    for variable, key in (
        ("http_proxy", "httpProxy"),
        ("https_proxy", "httpsProxy"),
        ("no_proxy", "noProxy"),
    ):
        value = os.environ.get(variable, os.environ.get(variable.upper()))
        if value is not None:
            proxies[key] = value
    if not proxies:
        return
    destination = Path(
        os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker"
    )
    destination.mkdir(parents=True, exist_ok=True)
    source = os.environ.get("STANDALONE_DOCKER_CONFIG_SOURCE")
    if source and Path(source).resolve() != destination.resolve():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    path = destination / "config.json"
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError("expected a JSON object")
        config.setdefault("proxies", {}).setdefault("default", {}).update(
            proxies
        )
    except (ValueError, AttributeError) as error:
        raise DockerError(
            f"Invalid Docker client configuration in {path}: {error}"
        ) from error
    path.write_text(json.dumps(config, indent=2) + "\n")
    path.chmod(0o600)


def select_endpoint():
    # docker-py does not understand Docker contexts. Resolve the CLI context
    # into the standard Docker environment used by both clients.
    if os.environ.get("DOCKER_CONTEXT") or not os.environ.get("DOCKER_HOST"):
        try:
            context = json.loads(
                subprocess.check_output(
                    ["docker", "context", "inspect", "--format", "{{json .}}"],
                    text=True,
                    timeout=10,
                )
            )
            endpoint = context["Endpoints"]["docker"]
            os.environ["DOCKER_HOST"] = endpoint["Host"]
            os.environ.pop("DOCKER_CERT_PATH", None)
            os.environ.pop("DOCKER_TLS_VERIFY", None)
            if context.get("TLSMaterial", {}).get("docker"):
                os.environ["DOCKER_CERT_PATH"] = str(
                    Path(context["Storage"]["TLSPath"]) / "docker"
                )
                os.environ["DOCKER_TLS_VERIFY"] = (
                    "" if endpoint.get("SkipTLSVerify") else "1"
                )
        except (subprocess.SubprocessError, ValueError, KeyError) as error:
            raise DockerError(
                "Cannot read the selected Docker context; check the Docker connection settings."
            ) from error
    os.environ.pop("DOCKER_CONTEXT", None)
    return os.environ["DOCKER_HOST"]


def docker_ready(timeout):
    try:
        result = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def local_endpoint():
    endpoint = os.environ["DOCKER_HOST"]
    if not endpoint.startswith("unix://"):
        raise DockerError(
            f"Docker daemon at '{endpoint}' is unreachable; check its address and credentials."
        )
    socket = Path(endpoint[len("unix://") :])
    if socket.exists() and stat.S_ISSOCK(socket.stat().st_mode):
        if not os.access(socket, os.R_OK | os.W_OK):
            raise DockerPermissionError(
                f"No permission to access Docker socket '{socket}'."
            )
        # A stale local socket can be replaced by dockerd; remote endpoints cannot.
    if os.geteuid() != 0:
        raise DockerPermissionError(
            "Starting a local Docker daemon requires root."
        )
    if shutil.which("dockerd") is None:
        raise DockerError(
            "Required command 'dockerd' is missing; install Docker Engine."
        )
    return endpoint, socket


def ensure_buildx():
    name = "default"

    def inspect(builder, bootstrap=False):
        try:
            return (
                subprocess.run(
                    ["docker", "buildx", "inspect", builder]
                    + (["--bootstrap"] if bootstrap else []),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60 if bootstrap else 10,
                    check=False,
                ).returncode
                == 0
            )
        except subprocess.TimeoutExpired:
            return False

    if not inspect(name, bootstrap=True):
        name = "cpython-standalone"
        if not inspect(name):
            args = [
                "docker",
                "buildx",
                "create",
                "--name",
                name,
                "--driver",
                "docker-container",
                "--driver-opt",
                "network=host",
                "--buildkitd-flags",
                "--allow-insecure-entitlement network.host",
            ]
            try:
                subprocess.run(
                    args + [os.environ["DOCKER_HOST"]], check=True, timeout=30
                )
            except (subprocess.SubprocessError, OSError) as error:
                raise DockerError(
                    f"Cannot create Buildx builder '{name}'."
                ) from error
        if not inspect(name, bootstrap=True):
            raise DockerError(
                f"Buildx builder '{name}' is unreachable; check Docker and BuildKit logs."
            )
    # Avoid changing the user's globally selected builder.
    os.environ["BUILDX_BUILDER"] = name


def stop_daemon(daemon):
    if daemon.poll() is None:
        daemon.terminate()
    try:
        daemon.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        daemon.kill()
        daemon.wait()


@contextmanager
def ensure_docker(config):
    """Reuse Docker or start a local daemon, then bootstrap Buildx."""
    if shutil.which("docker") is None:
        raise DockerError("Required command 'docker' is missing from PATH.")
    previous = {
        name: os.environ.get(name)
        for name in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CERT_PATH",
            "DOCKER_TLS_VERIFY",
            "BUILDX_BUILDER",
        )
    }
    daemon = None
    try:
        configure_proxy()
        select_endpoint()
        if not docker_ready(timeout=10):
            endpoint, socket = local_endpoint()
            socket.parent.mkdir(parents=True, exist_ok=True)
            EXEC_ROOT.mkdir(parents=True, exist_ok=True)
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            args = [
                "dockerd",
                "--config-file=/dev/null",
                f"--host={endpoint}",
                f"--data-root={DATA_ROOT}",
                f"--exec-root={EXEC_ROOT}",
                f"--pidfile={EXEC_ROOT / 'dockerd.pid'}",
                "--shutdown-timeout=10",
            ]
            mirror = os.environ.get(
                "STANDALONE_DOCKER_REGISTRY_MIRROR",
                "https://docker.m.daocloud.io",
            )
            if mirror:
                args += ["--registry-mirror", mirror]
            print(f"Starting Docker daemon; log: {LOG_PATH}", flush=True)
            with LOG_PATH.open("w") as log:
                daemon = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            deadline = time.monotonic() + READY_TIMEOUT
            while not docker_ready(timeout=5):
                if daemon.poll() is not None or time.monotonic() >= deadline:
                    with LOG_PATH.open(errors="replace") as log:
                        sys.stderr.writelines(deque(log, maxlen=100))
                    raise DockerError(
                        "Docker did not become ready; check its log and container --privileged setting."
                    )
                time.sleep(0.5)
            print("Docker daemon is ready.", flush=True)
        ensure_buildx()
        yield config
    finally:
        with ignore_interrupts():
            try:
                if daemon is not None:
                    stop_daemon(daemon)
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
