#!/usr/bin/env bash
# Launch the Python driver inside the build image without host Python.
set -euo pipefail

# Resolve host paths before passing them to the container.
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work_dir=$(realpath -m -- "${CUSTOM_STANDALONE_WORK_DIR:-${source_dir}/../.cpython-standalone}")
output_dir=$(realpath -m -- "${STANDALONE_OUTPUT_DIR:-${source_dir}/output}")
build_image=hub.byted.org/compile/bytedpython_standalone_build_base_image:5ac43490752eb7eb0be93df56caec3f4

command -v docker >/dev/null || {
    echo 'Error: Container builds require Docker on the host.' >&2
    exit 1
}

# Retain the original caller's identity and Docker config when invoked with sudo.
caller_uid=${SUDO_UID:-$(id -u)}
caller_gid=${SUDO_GID:-$(id -g)}
IFS=: read -r _ _ _ _ _ caller_home _ < <(getent passwd "${caller_uid}")
docker_config=$(realpath -m -- "${DOCKER_CONFIG:-${caller_home}/.docker}")
docker=(docker --config "${docker_config}")

# Resolve the selected endpoint; bind mounts require a local Docker daemon.
if [[ -n ${DOCKER_CONTEXT:-} || -z ${DOCKER_HOST:-} ]]; then
    endpoint=$("${docker[@]}" context inspect --format '{{.Endpoints.docker.Host}}')
else
    endpoint=${DOCKER_HOST}
fi
if [[ ${endpoint} != unix://* ]]; then
    echo 'Error: Container builds require local Docker for source bind mounts.' >&2
    exit 1
fi

# Elevate only the Docker command if the caller cannot access its socket.
if [[ ! -r ${endpoint#unix://} || ! -w ${endpoint#unix://} ]]; then
    command -v sudo >/dev/null || {
        echo 'Error: No permission to access the host Docker socket.' >&2
        exit 1
    }
    docker=(sudo -n -E "${docker[@]}")
fi
"${docker[@]}" info >/dev/null

# The image entrypoint starts an inner Docker daemon for PBS compilation.
args=(run --rm --privileged --network=host --workdir "${source_dir}")

# Mount each parent once at its original path, keeping locks and atomic output
# replacement on the host and making source paths usable by the inner daemon.
declare -A mounted=()
for directory in "${source_dir}" "${work_dir}" "${output_dir}"; do
    parent=$(dirname -- "${directory}")
    if [[ ${parent} == / ]]; then
        echo "Error: Container source, work directory and output need parent directories below '/'." >&2
        exit 1
    fi
    if [[ -z ${mounted["${parent}"]+present} ]]; then
        mkdir -p -- "${parent}"
        args+=(--mount "type=bind,src=${parent},dst=${parent}")
        mounted[${parent}]=1
    fi
done

# Forward build settings, package indexes, proxies, and Git SSH options.
while IFS= read -r key; do
    case ${key} in
        UV_PROJECT_ENVIRONMENT) ;; # The container uses its own venv below.
        PIP_*|UV_*|http_proxy|https_proxy|no_proxy|HTTP_PROXY|HTTPS_PROXY|NO_PROXY|GIT_SSH_COMMAND|STANDALONE_DOCKER_REGISTRY_MIRROR|CUSTOM_STANDALONE_BUILD_BRANCH|CUSTOM_COMPILER_OPTIONS|CUSTOM_INSTALL_REQUIREMENTS|BUILD_VERSION|PYBUILD_RELEASE_TAG)
            args+=(--env "${key}=${!key}") ;;
    esac
done < <(compgen -e)

# Use the inner daemon, retain output ownership, and isolate uv's venv.
args+=(
    --env "CUSTOM_STANDALONE_WORK_DIR=${work_dir}"
    --env "STANDALONE_OUTPUT_DIR=${output_dir}"
    --env DOCKER_HOST=unix:///var/run/docker.sock
    --env "SUDO_UID=${caller_uid}" --env "SUDO_GID=${caller_gid}"
    --env UV_PROJECT_ENVIRONMENT=/run/standalone/pbs-venv
)

command=(python3 "${source_dir}/standalone/build_standalone.py")

# Copy read-only SSH files into root's writable home for Git access; also mount
# the caller's SSH path for configurations that refer to files there explicitly.
if [[ -d ${caller_home}/.ssh ]]; then
    args+=(--mount "type=bind,src=${caller_home}/.ssh,dst=/run/standalone-ssh,readonly")
    if [[ ${caller_home} != /root ]]; then
        args+=(--mount "type=bind,src=${caller_home}/.ssh,dst=${caller_home}/.ssh,readonly")
    fi
    command=(bash -c 'mkdir -p /root/.ssh && cp -r /run/standalone-ssh/. /root/.ssh/ && exec "$@"' standalone "${command[@]}")
fi

# Forward an available SSH agent so Git can use keys already loaded on the host.
if [[ -n ${SSH_AUTH_SOCK:-} && -S ${SSH_AUTH_SOCK} ]]; then
    args+=(--mount "type=bind,src=${SSH_AUTH_SOCK},dst=/run/standalone-ssh-agent"
        --env SSH_AUTH_SOCK=/run/standalone-ssh-agent)
fi

# Expose the host Docker config read-only for the container's configuration setup.
if [[ -d ${docker_config} ]]; then
    args+=(--mount "type=bind,src=${docker_config},dst=/run/standalone-docker-source,readonly"
        --env STANDALONE_DOCKER_CONFIG_SOURCE=/run/standalone-docker-source)
fi

echo "[standalone] Build in container: ${build_image}"
# Replace the launcher with Docker so its exit status reaches the caller.
exec "${docker[@]}" "${args[@]}" "${build_image}" "${command[@]}"
