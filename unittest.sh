#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
export INSTALLDIR="${INSTALLDIR:-/usr/local/python${PYTHON_VERSION}}"
export PATH="${INSTALLDIR}/bin/:$PATH"

verify_bundled_package_versions() {
    export VERIFY_DIR="${VERIFY_DIR:-/tmp/verify_packages}"
    rm -rf "${VERIFY_DIR}" && mkdir -p "${VERIFY_DIR}"
    cd "${VERIFY_DIR}"

    python -c "import packaging; assert packaging.__version__ == '25.0', f'packaging wheel: {packaging.__version__}'"
    python -c "import setuptools; assert setuptools.__version__ == '80.9.0', f'setuptools version:{setuptools.__version__}'"
    python -c "import wheel; assert wheel.__version__ == '0.45.1', f'wheel version:{wheel.__version__}'"
    python -c "import setuptools; from wheel.bdist_wheel import bdist_wheel"
    python -c "from packaging import version; assert version.parse('1.0') < version.parse('2.0a1'), 'version parse error'"
    python -c "from packaging.specifiers import SpecifierSet; spec = SpecifierSet('>=1.0,<2.0'); assert '1.5' in spec and '2.1' not in spec, 'spec parse error.'"
}

verify_byted_version_flags() {
    [ -n "${BUILD_VERSION:-}" ] || return 0

    local base scm expected actual
    base="$(sed -n 's/^#define PY_VERSION *"\(.*\)"/\1/p' "${SRC_DIR:-${SCRIPT_DIR}}/Include/patchlevel.h")"
    scm="${BUILD_VERSION##*.}"
    expected="Python ${base}.bm.1.0.${scm}"

    for switch in --BV --byted-version; do
        actual="$(python "${switch}")"
        echo "byted version flag '${switch}': ${actual}"
        # Everything up to the " (<buildinfo>) <compiler>" tail must equal expected.
        [ "${actual%% (*}" = "${expected}" ] || {
            echo "byted version flag '${switch}': expected prefix '${expected}', got '${actual}'" >&2
            exit 1
        }
    done
}

run_cpython_official_regression_tests() {
    export REGRTEST_DIR="${REGRTEST_DIR:-/tmp/regrtest_workdir}"
    export SRC_DIR="${SRC_DIR:-${SCRIPT_DIR}}"
    rm -rf "${REGRTEST_DIR}" && mkdir -p "${REGRTEST_DIR}"

    # test.test_concurrent_futures.test_process_pool: https://github.com/python/cpython/pull/131108
    sed -i "69s/self.assertEqual(\[None, None\], results)/self.assertIn(results, (\[None, None\], \[None\], \[\]))/g" "${SRC_DIR}/Lib/test/test_concurrent_futures/executor.py"

    cp -r "${SRC_DIR}/Lib/test" "${INSTALLDIR}/lib/python${PYTHON_VERSION}/"
    cp -r "${SRC_DIR}/Lib/idlelib/idle_test" "${INSTALLDIR}/lib/python${PYTHON_VERSION}/idlelib"

    cd "${REGRTEST_DIR}"
    # Pin affinity to the online CPU set: some runners expose an affinity mask
    # wider than the online CPUs, making os.process_cpu_count() exceed
    # os.cpu_count() and breaking test_os.CPUCountTests.
    local ncpu cpu_range
    ncpu="$(getconf _NPROCESSORS_ONLN)"
    cpu_range="0-$((ncpu - 1))"
    echo "Restricting CPU affinity to: ${cpu_range}"

    # Tests excluded by platform in the reference pipelines.
    local -a excluded
    if [ "$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}")" = "bullseye" ]; then
        echo "Detected Debian bullseye: excluding its reference test set."
        excluded=(test_multiprocessing_forkserver test_pyexpat test_sax test_socket test_timeout test_urllib2net test_xml_etree test_xml_etree_c test_urllib2 test_smtpnet test_tracemalloc)
    elif [ "$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}")" = "bookworm" ] || [ "$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}")" = "trixie" ] || [ "$(. /etc/os-release 2>/dev/null && echo "${ID:-}")" = "velinux" ]; then
        echo "Detected Debian bookworm/trixie or Velinux: excluding their reference test set."
        excluded=(test_urllib2net test_socket test_ssl test_multiprocessing_forkserver test_timeout test_urllib2)
    else
        excluded=()
    fi

    local -a regrtest_args=(--slow-ci)
    if ((${#excluded[@]})); then
        regrtest_args+=(-x "${excluded[@]}")
    fi

    taskset -c "${cpu_range}" \
        env LD_LIBRARY_PATH="${INSTALLDIR}/lib" \
        "${INSTALLDIR}/bin/python3" -m test "${regrtest_args[@]}"
}

verify_byted_version_flags
verify_bundled_package_versions
run_cpython_official_regression_tests
