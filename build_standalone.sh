#!/usr/bin/env bash
# Run the same Python build flow on a development host or in SCM CI.
set -euo pipefail

workdir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ $# -gt 0 ]]; then
    if [[ $1 == --help || $1 == -h ]]; then
        cat "${workdir}/standalone/usage.txt"
        exit 0
    fi
    echo 'Error: Unexpected arguments. Use --help for environment variables.' >&2
    exit 1
fi

if [[ ${CUSTOM_STANDALONE_BUILD_IMAGE:-} == true ]]; then
    exec bash "${workdir}/standalone/container_build.sh"
else
    exec python3 "${workdir}/standalone/build_standalone.py" "$@"
fi
