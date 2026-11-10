<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Development and case-study guide

How to set up the environment, run the test suite and rerun the case
studies. It separates a minimal smoke test, which needs only the
[prerequisites in the README](../README.md#prerequisites), from the full
evaluation, which needs third-party installs.

## Prerequisite matrix

| What you want | Needs | Result |
|---|---|---|
| Smoke test: `examples/hello` ([quickstart](../README.md#quickstart)) | Python 3.12+, uv, Ninja, the patched Soufflé (`BUILDLOG_SOUFFLE`), a C++ compiler, zlib with `pkg-config` | A built `hello` |
| Test suite, minimal environment | the above | Core tests run; platform and external-install tests may skip |
| Full suite | the above, `git submodule update --init`, [`fetch-deps.sh`](../case-studies/spirv-tools/fetch-deps.sh), and the variables in the next section | everything runs, with a few skips that depend on the platform |
| Case studies | the case studies' submodules; Halide, SPIRV-Headers, Qt and nanobind need the installs below | see [Case studies](#case-studies) |
| Linux verification | Docker, a checkout of the patched Soufflé | see [linux/README.md](../linux/README.md) |

The skips in the minimal environment are tests that need an install the
variables below point at (Halide, Qt, SPIRV-Headers, nanobind virtual
environments), the SPIRV-Tools dependencies, or a Linux host.

## Environment variables

[`env.local.sh.example`](../env.local.sh.example) lists them. Copy it to
`env.local.sh` (gitignored), fill it in, and load it with
`set -a; . ./env.local.sh; set +a`. An unset variable makes the tests that
depend on it skip.

| Variable | Meaning |
|---|---|
| `BUILDLOG_SOUFFLE` | The patched Soufflé binary. Required by everything. |
| `BUILDLOG_HALIDE_PREFIX` | An install prefix (`cmake --install ... --prefix DIR`) of a build of `case-studies/halide/upstream`. The tests forward it to buildlog as `-D HALIDE_PREFIX=...`. |
| `BUILDLOG_HALIDE_CMAKE_TREE` | The CMake release build tree from `case-studies/halide/baseline/run-cmake.sh`. Default `build/case-halide/cmake-release`. |
| `BUILDLOG_HALIDE_BUILD` | Set to `1` to let the Halide tests run the full build. |
| `EMSDK` | An emsdk install (3.1.73) for the HelloWasm study (`case-studies/halide/apps/`); the tests skip without it. |
| `EM_BINARYEN_ROOT` | A binaryen newer than emsdk's bundled one (tested with version_123), which the wasm build needs for the features Halide's LLVM emits. |
| `ANDROID_NDK_ROOT` | An Android NDK (tested with 30.0.15729638) for the HelloAndroid study; the tests skip without it. |
| `BUILDLOG_SPIRV_HEADERS_PREFIX`, `BUILDLOG_SPIRV_HEADERS_ALT_PREFIX` | Install prefixes of SPIRV-Headers at the pinned commit and at the alternate tag, for the SPIRV-Tools study. |
| `BUILDLOG_QT_PREFIX` | A Qt 6 macOS prefix (`.../6.11.3/macos`) for the Qt moc study. |
| `BUILDLOG_NANOBIND_VENVS` | A directory holding `venv313/` and `venv311/`, for the nanobind study. Default `build/case-nanobind`. |

buildlog itself reads a few more, all optional:

| Variable | Meaning |
|---|---|
| `BUILDLOG_CACHE_DIR` | Where the Soufflé analysis cache lives. `off` disables it. Default: `~/Library/Caches/buildlog` on macOS, `$XDG_CACHE_HOME/buildlog` elsewhere. |
| `BUILDLOG_PACKAGE_PATH` | Extra prefixes searched for rule packages. |
| `BUILDLOG_LLVM_BIN` | The `bin` directory of an upstream LLVM install; if it holds `clang++`, the toolchain inventory offers it as a toolchain. |
| `BUILDLOG_LINUX_TOOLCHAINS` | On Linux, which toolchain family (`gcc` or `clang`) an unpinned configure finds. Default `gcc`. |
| `BUILDLOG_SEED_PROBES=0`, `BUILDLOG_SOUFFLE_SERVE=0`, `BUILDLOG_ROUND_MEMO=0`, `BUILDLOG_SOUFFLE_DEFER=0` | Turn off probe seeding, the long-lived Soufflé process, round memoisation, and deferred output of the discovery rounds, for debugging. See [Caches and speed](user-guide.md#caches-and-speed). |

`BUILDLOG_<VAR>` is also how a spec variable `<VAR>` is overridden from the
environment, but only for variables the spec declares in `[vars]`. The
third-party variables above are not such overrides: the tests read them and
pass the value on with `-D`.

## SPIRV-Tools dependencies

```sh
git submodule update --init
bash case-studies/spirv-tools/fetch-deps.sh
```

The script populates `case-studies/spirv-tools/upstream/external/` (googletest,
effcee, re2, abseil) at the revisions the study pins.

## The test suite

```sh
uv sync --frozen
set -a; . ./env.local.sh; set +a
uv run --frozen pytest -q -n 8
```

`pytest` runs with `-n auto` by default (pytest-xdist), so pass `-n` to cap the
workers. Tests configure into `tmp_path`; a session fails if it changes a
tracked or unignored file in the checkout. Tests marked `integration` configure
and build real projects, and `-m "not integration"` skips them for a faster run.
`tests/test_locks.py` checks that every committed `buildlog.lock` is current.

Fixture projects live in `tests/projects/`. The tests for the runnable
examples are in `tests/test_example_projects.py`.

To make `git commit` run the formatters and linters (ruff, tombi):
`uv run pre-commit install`.

## Case studies

Each directory under `case-studies/` has a README with the question it asks,
the pinned upstream (a submodule in `upstream/`), how to rerun it, and its
results. Most have a `baseline/` with the upstream build's own measurements.

| Directory | Subject |
|---|---|
| `halide/` | Halide, built from facts and compared with its CMake build. The largest study. |
| `zlib/` | Configure-time probes, `configure_file`, shared and static libraries from one source set. |
| `zlib-ng/` | Per-source ISA flags and ISA compatibility. |
| `ggml/` | The CPU backend as per-ISA modules. |
| `nanobind/` | A Python extension module (needs `BUILDLOG_NANOBIND_VENVS`). |
| `qt-moc/` | Qt's moc as a rule package a library ships to its consumers (needs `BUILDLOG_QT_PREFIX`). |
| `spirv-tools/` | Generated tables that depend on imported SPIRV-Headers data (needs the SPIRV-Headers prefixes and `fetch-deps.sh`). |
| `fribidi/` | A Meson baseline; host programs that generate tables. |
| `flatbuffers/`, `bazel-flatc/` | A code generator the build runs, and a comparison with a Bazel build. |
| `nlohmann-json/` | Install and export of a header-only library. |

The tests for each study are `tests/test_<name>_case.py` (Halide:
`tests/test_halide_*.py`). The Halide study has its own Docker harness in
[`case-studies/halide/docker/`](../case-studies/halide/docker/README.md).

## Linux harness

[`linux/`](../linux/README.md) runs the suite and the case studies in one
Ubuntu 24.04 image (GCC 13, Clang 18, LLVM 22; versions pinned and listed in
`/etc/buildlog-versions`). `linux/run.sh suite|cases gcc|clang` builds and
tests zlib, nlohmann-json, zlib-ng, ggml, fribidi, flatbuffers,
SPIRV-Tools, nanobind (uv-managed CPython 3.13 and 3.11) and, with GCC,
libHalide against LLVM 22; `linux/run.sh baselines gcc|clang` runs each study's upstream
build (CMake, Meson) on Linux into `case-studies/*/baseline/linux{,-clang}/`,
which the tests compare against by host, as on macOS. Each study's README has
a "Linux" section with its deviations. Results from a run are in
the gitignored `linux/raw/` directory.
