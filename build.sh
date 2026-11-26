#!/bin/bash -ex
# Build cpython from source

PYVERSION="3.13"

WORKDIR=$(pwd)
OUTPUTDIR=${WORKDIR}/output

if [ "${BUILD_FOR_TCE_BASE}" = "true" ]; then
    INSTALL_PREFIX=/usr/local
else
    if [ -d "${OUTPUTDIR}" ]; then
        echo "Directory ${OUTPUTDIR} already exists, cannot pack artifacts! Removing the old ${OUTPUTDIR} and creating a new one."
        rm -rf ${OUTPUTDIR}
    fi
    mkdir -p ${OUTPUTDIR}

    INSTALL_PREFIX=/usr/local/python${PYVERSION}
    if [ -d "${INSTALL_PREFIX}" ]; then
        echo "Directory ${INSTALL_PREFIX} already exists, cannot install the same version of Python!"
        exit 1
    fi
fi

function build_from_source() {
    gnuArch="$(dpkg-architecture --query DEB_BUILD_GNU_TYPE)"
    ./configure \
      --build="${gnuArch}" \
      --enable-loadable-sqlite-extensions \
      --enable-optimizations \
      --enable-option-checking=fatal \
      --enable-shared \
      --prefix="${INSTALL_PREFIX}" \
      --with-lto \
      --with-system-expat \
      --with-ensurepip=install
    EXTRA_CFLAGS="$(dpkg-buildflags --get CFLAGS)"
    LDFLAGS="$(dpkg-buildflags --get LDFLAGS)"
    LDFLAGS="${LDFLAGS:--Wl},--strip-all"
    nproc="$(nproc)"

    make -j "${nproc}" \
      "EXTRA_CFLAGS=${EXTRA_CFLAGS:-}" \
      "LDFLAGS=${LDFLAGS:-}" \
      "PROFILE_TASK=${PROFILE_TASK:-}"

    # https://github.com/docker-library/python/issues/784
    # prevent accidental usage of a system installed libpython of the same version
    rm python
    make -j "${nproc}" \
      "EXTRA_CFLAGS=${EXTRA_CFLAGS:-}" \
      "LDFLAGS=${LDFLAGS:--Wl},-rpath='\$\$ORIGIN/../lib'" \
      python

    LD_LIBRARY_PATH=${WORKDIR}:${LD_LIBRARY_PATH} ${WORKDIR}/python --version
}

function make_install() {
    make install

    # keep source code, but we may clean unnecessary packages later to reduce image size if needed
    # clean-up intermediate files
    cd /
    find ${INSTALL_PREFIX} -depth \
    \( \
        \( -type d -a \( -name test -o -name tests -o -name idle_test \) \) \
        -o \( -type f -a \( -name '*.pyc' -o -name '*.pyo' -o -name 'libpython*.a' \) \) \
    \) -exec rm -rf '{}' +

    # make some useful symlinks that are expected to exist ("${INSTALL_PREFIX}/bin/python" and friends)
    for src in idle3 pydoc3 python3 python3-config; do
        dst="$(echo "$src" | tr -d 3)"
        [ -s "${INSTALL_PREFIX}/bin/$src" ]
        [ ! -e "${INSTALL_PREFIX}/bin/$dst" ]
        ln -svT "$src" "${INSTALL_PREFIX}/bin/$dst"
    done

    ln -s ${INSTALL_PREFIX}/bin/pip3 ${INSTALL_PREFIX}/bin/pip

    ${INSTALL_PREFIX}/bin/pip install -r ${WORKDIR}/requirements.txt

    ${INSTALL_PREFIX}/bin/python --version
    ${INSTALL_PREFIX}/bin/python3 --version
    ${INSTALL_PREFIX}/bin/pip --version
    ${INSTALL_PREFIX}/bin/pip3 --version
}

function copy_to_output_dir() {
    if [ ! -d ${OUTPUTDIR} ]; then
        echo "Error: Directory ${OUTPUTDIR} does not exist!"
        exit 1
    fi

    cp -r ${INSTALL_PREFIX}/* ${OUTPUTDIR}/
}

build_from_source
if [ "${BUILD_FOR_TCE_BASE}" = "true" ]; then
    make install
elif [ "${IS_DOCKER_ENV}" = "true" ]; then
    make_install
    copy_to_output_dir
fi
