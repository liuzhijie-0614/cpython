# Standalone CPython builds

Run `./build_standalone.sh` from the CPython checkout, or invoke it by path from
another directory. The source is always the CPython checkout containing the script.
The shell entry selects host or container mode before starting Python; development
hosts and SCM jobs then run the same Python build flow. PBS uses Docker for compilation.
Only the shell entry selects container mode; the Python driver always builds
in its current environment.

The `standalone/` directory contains the build driver, shell container launcher
(`container_build.sh`), Docker setup (`standalone_docker.py`), installation checks
(`verify_standalone_install.py`), shared help text (`usage.txt`), regression tests
(`test_standalone_build.py`), and this documentation.
The shell entry remains at the checkout root.

The driver first prepares `git@code.byted.org:lang/python-build-standalone.git`.
The default branch is always `release`; `CUSTOM_STANDALONE_BUILD_BRANCH` overrides
it. A missing checkout is cloned. An existing checkout switches to the selected
branch, fetching that branch from origin if it is not available locally. Before
switching, tracked local edits are saved in Git stash with the original branch
and stash commit printed. Untracked build caches remain in place. Saved edits
can be reapplied on their original branch with `git stash apply --index <printed-commit>`.

## Environment

Set `CUSTOM_STANDALONE_BUILD_IMAGE=true` to run the build driver inside a container.
It defaults to building directly on the host; only `true` enables container mode.
Container mode needs Bash, standard Linux shell utilities, and a running local
Docker daemon on the host. It uses Python, uv, Buildx, and the other build tools
from the image, so host Python is not required. Host mode requires Python 3.
Missing tools are reported rather than installed automatically.

The image is fixed in the shell container launcher to
`hub.byted.org/compile/bytedpython_standalone_build_base_image:5ac43490752eb7eb0be93df56caec3f4`,
the domestic [ICM 2542694](https://cloud.bytedance.net/icm/detail/2542694/versions?x-resource-account=public&x-bc-region-id=bytedance)
version `1.0.0.7`, which supports x86_64 and aarch64:

```bash
CUSTOM_STANDALONE_BUILD_IMAGE=true ./build_standalone.sh
```

The container uses the image entrypoint and its own Docker daemon, with
`--privileged --network=host`. Source, builder cache/locks, and output parent
directories are mounted at their original paths so the published install stays
on the host. Build settings, proxies, and pip/uv indexes are passed through.
The container's uv project environment is fixed at `/run/standalone/pbs-venv`,
so it does not replace the host builder's `.venv`.
SSH files and Docker client configuration are mounted read-only and copied into
container-owned storage; an available SSH agent is forwarded. The final install
is owned by the original caller. The container is removed when it exits, and
its exit status is returned by the entry.

For host mode, use Linux x86_64 or aarch64 with Python 3.9 or newer, git, uv, Docker Engine,
the Docker Buildx plugin, make, and tar. Missing tools produce an installation
error. The script checks Docker and bootstraps Buildx before compiling.

An existing Docker daemon is reused. If a local daemon is not running, the
script starts `dockerd`, waits up to 60 seconds, and stops only that daemon
when the build exits. Starting Docker requires root; inside an SCM container
it also requires `--privileged`. Its log is `/var/log/standalone-dockerd.log`.
An unreachable remote daemon produces an error.

On a development host, insufficient Docker socket permissions or a daemon
startup requiring root cause the complete build to restart with `sudo -n -E`.
This requires permission to use sudo without a password and preserve the
environment. The caller's PATH and Docker client configuration are retained.
The newly published install is owned by the original caller. Fix the user's
Docker access or run the entry with the appropriate privileges when sudo is
unavailable. PBS's cache can be written by root after elevation.

The existing Docker connection settings, including contexts and TLS, are
honored. The Docker CLI and PBS's Python Docker SDK use the same endpoint.
The SDK is checked in PBS's actual uv environment.
The default Buildx builder is reused when reachable. A missing/unreachable
default builder falls back to a dedicated `cpython-standalone` builder at the
same daemon. Buildx selection is scoped to the build's environment.
Image `RUN` steps and a newly created Buildx builder use the daemon host's
network so nested CI builds can reach the same DNS and package services.
The script manages the network and Buildx selection automatically.

Proxies and uv/pip indexes use the existing environment. Explicitly empty
proxy values remain disabled. Docker client proxy settings
are updated only when proxy variables are provided; registry credentials and
other client settings are preserved.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CUSTOM_STANDALONE_BUILD_IMAGE` | Disabled | Only `true` runs in the fixed ICM image; other values build on the host |
| `CUSTOM_STANDALONE_WORK_DIR` | `<cpython-parent>/.cpython-standalone` | Standalone work root containing `python-build-standalone/` and `work/`; must be outside CPython |
| `CUSTOM_STANDALONE_BUILD_BRANCH` | `release` | Branch to clone or check out in the existing PBS directory |
| `CUSTOM_COMPILER_OPTIONS` | `pgo+lto` | `noopt`, `debug`, `pgo`, `lto`, or `pgo+lto`, optionally prefixed with `freethreaded+` or `free-threaded+` |
| `CUSTOM_INSTALL_REQUIREMENTS` | Disabled | Only `true` enables installing and verifying this checkout's `requirements.txt`; other values disable it |
| `BUILD_VERSION` | Unset | Byted version passed through PBS's `--build-version` |
| `STANDALONE_OUTPUT_DIR` | `<current checkout>/output` | Uncompressed installation; may be inside CPython, and must not overlap the PBS checkout or contain CPython |
| `PYBUILD_RELEASE_TAG` | UTC timestamp | PBS internal archive tag |

`http_proxy`, `https_proxy`, `no_proxy` and their uppercase forms configure
proxies. Standard `UV_*` and `PIP_*` index settings configure package downloads.
`STANDALONE_DOCKER_REGISTRY_MIRROR` configures a daemon started by this script;
the default is `https://docker.m.daocloud.io`, and an empty value disables it.

To include this checkout's pinned packages in the install tree:

```bash
CUSTOM_INSTALL_REQUIREMENTS=true ./build_standalone.sh
```

`BUILD_VERSION` requires a PBS branch with `--build-version` support. The current
reference checkout provides it on `feat/build-version-asyncio-patch`. To reuse
that checkout:

```bash
CUSTOM_STANDALONE_BUILD_BRANCH=feat/build-version-asyncio-patch \
CUSTOM_COMPILER_OPTIONS=noopt BUILD_VERSION=1.0.0.5 ./build_standalone.sh
```

SCM invokes the same entry. Supply tools in its build image and configure its
daemon/socket access, repository SSH credentials, proxy, and index environment.
If the image entrypoint is overridden, the Python driver still initializes
Docker. No development/SCM mode variable is needed.

## Build and output

1. Lock and prepare the selected PBS checkout.
2. Check tools, connect Docker, bootstrap Buildx, and test the Python Docker SDK.
3. Build with `uv run --no-dev build.py --python-source <cpython>`, using the source
   checkout directly, including local edits, ignored files, and `.git`.
   Source files are not copied to staging. PBS's checkout and build
   cache stay outside the source tree.
   Force CPython and host-Python rebuilds so changed source and version settings
   cannot use old interpreter artifacts; retain dependency caches. Use PBS's fast compression
   profile for its extra internal archive; the delivered install stays uncompressed.
4. Extract `python/install` and metadata from PBS's `build/*.tar`, then run its
   distribution runtime tests.
5. When `CUSTOM_INSTALL_REQUIREMENTS=true`, install this checkout's
   `requirements.txt` using the built interpreter, then make the installed
   console scripts relocatable. This step is skipped by default.
6. Verify the staged install, replace the output directory, and verify its final
   path. Restore the previous output if publication or final verification fails.

Checks compare both `bin/python3` and `bin/python` outputs for `-I --version`
and `-I --BV` against the expected Python and Byted versions. Their `-I -VV`
outputs must also match the verifying interpreter's complete version string.
They also verify installation prefixes, debug/free-threaded settings, pip's installation
location, `pip check`, and relocated `pip` and `pip3` commands.
When the source contains `.git`, embedded Git metadata is required: the revision
must match the source's `HEAD`, and the identifier must match
`git describe --all --always`. The identifier may have an additional `-dirty`
suffix because PBS patches its build copy. Missing metadata or a mismatch fails
verification. Compilation images must provide Git. Source archives without
`.git` retain version checks without a source Git comparison.
With `CUSTOM_INSTALL_REQUIREMENTS=true`, checks also verify the pinned package
versions and locations from `requirements.txt` and the relocated `wheel` command.
The delivered output is an installation directory. PBS retains its own archives
in its checkout. A custom existing output must contain `.standalone-build-output`
before the driver will replace it. The conventional checkout `output/` is treated
as the build output, matching `build.sh`.

The hidden `.cpython-standalone/` directory beside the CPython checkout contains
`python-build-standalone/` for the default PBS checkout and dependency cache, and
`work/` for each build's temporary directory. The PBS checkout and cache are
retained; each temporary directory is removed on exit. `CUSTOM_STANDALONE_WORK_DIR`
overrides this entire root, so the PBS checkout and temporary directories move
together. The work root must stay outside CPython and must not contain it.
Publication staging stays beside the configured output so replacement uses
atomic renames.

For example, `CUSTOM_STANDALONE_WORK_DIR=/data/standalone ./build_standalone.sh`
uses `/data/standalone/python-build-standalone/` for PBS and
`/data/standalone/work/` for temporary builds. Relative work roots are resolved
from the caller's working directory in both host and container modes.

PBS packages the source directory recursively, including any existing `output/`
or other local artifacts inside it. To keep the final install outside the source
tree as well, set `STANDALONE_OUTPUT_DIR=../cpython-standalone-output` when running
from the CPython checkout. The driver does not patch the PBS checkout.

Build failures and interrupts stop child processes and restore the previous
output before daemon cleanup. Temporary work and staging directories are removed;
a backup is retained with its path reported if restoration fails.

PBS build-time metadata checks and distribution runtime tests run by default.
The separate Rust `pythonbuild validate-distribution` checks and CPython's full
standard-library test suite are not run by this wrapper.

Run the focused regression checks with:

```bash
python3 standalone/test_standalone_build.py
```
