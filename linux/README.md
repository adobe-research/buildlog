<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Linux verification harness

buildlog's presets and tests name the macOS platform and toolchains
(`arm64-macos`, `apple-clang`). This directory checks that the suite and the
case studies also work on Linux, inside a Docker container.

What it does:

1. **`image`** builds the one Ubuntu 24.04 image every Linux run uses (GCC 13 and 14 (`g++-14`, for
   C++ modules: `tests/test_cxx_modules.py` puts it first on `PATH`), Clang 18, LLVM/Clang/LLD 22 from apt.llvm.org, CMake 4.4.3, Ninja 1.13.2 and
   Meson 1.12.1 from pip, uv, flatbuffers 25.12.19, wabt 1.0.42, googletest
   1.18.0, SPIRV-Headers, the dev libraries Halide needs). It also builds the
   patched Soufflé from a checkout you supply. Versions are pinned and listed
   in `/etc/buildlog-versions` (also printed at the end of the build and kept
   in `linux/raw/versions.txt`). The Halide Linux harness
   ([`case-studies/halide/docker/`](../case-studies/halide/docker/README.md))
   needs the same LLVM 22 and flatbuffers, which are in this image. Tier 3-4
   layers: Qt 6.11.3 (aqt, `/opt/qt`), Emscripten 3.1.73 (emsdk) with
   binaryen version_123, Bazel 8.8.1, an installed Halide (`/opt/halide`, built from the
   submodule's `git archive`) and the Android NDK r30-beta2. The NDK's host
   tools are x86_64 only: the image stays arm64 and runs them through Docker
   Desktop's binfmt x86_64 emulation, with amd64 `libc6`, `libstdc++6` and
   `zlib1g` from apt multiarch (one image, no amd64 sibling: nothing else
   needs x86_64, and the NDK plugin only spawns the NDK's clang). On an x86_64
   host the same Dockerfile needs no emulation. Image size 11.2 GB (4.71 before).
2. **Copies the worktree** into the container, so Linux build directories,
   virtual environments and locks never touch your macOS ones.
3. **Rewrites the platform and toolchain names** in that copy with
   [`port.py`](port.py) (the macOS platform becomes the Linux one, and
   `apple-clang` becomes `gcc` or `clang`), and drops a `.ported` marker that
   `tests/test_locks.py` recognises.
4. **Runs** the pytest suite (`suite`) or the case studies (`cases`, via
   [`cases.sh`](cases.sh)) with the GCC or the Clang toolchain.

## Running it

Prerequisites: Docker, `git submodule update --init` in the repository (the
case-study sources are copied into the container; Halide's, 2 GB, is mounted from the host
checkout and copied only by `cases gcc halide`), and a
checkout of the patched Soufflé (see the [README](../README.md#prerequisites)).

```sh
export SOUFFLE_SRC=/path/to/souffle         # the patched Soufflé checkout
export SOUFFLE_REF=<branch-or-commit>       # default: main
linux/run.sh image                          # build the image (JOBS=8, CPUS=8 by default)
linux/run.sh suite gcc                      # the pytest suite with GCC
linux/run.sh suite clang -k zlib            # extra arguments go to pytest
linux/run.sh cases gcc                      # the case studies, one PASS/FAIL line each
linux/run.sh cases clang ggml fribidi       # only these studies
linux/run.sh baselines gcc                  # upstream builds on Linux (CMake, Meson)
linux/run.sh baselines clang zlib-ng        # ... with Clang, for one study
linux/run.sh baselines gcc bazel-flatc halide-apps   # Bazel (and buildlog's comparison), the apps' CMake builds
linux/run.sh exec gcc "CMD"                 # a command in the ported copy
linux/run.sh shell                          # a shell in the container
linux/run.sh                                # image, both suites, both case-study runs
```

Logs go to `linux/raw/` (gitignored). `run.sh baselines` runs each study's own
baseline script (`case-studies/*/baseline/run-cmake.sh`, `run-meson.sh`; see
[`baselines.sh`](baselines.sh)) in the container and copies the outputs back
into the worktree: `baseline/linux/` for GCC and `baseline/linux-clang/` for
Clang, for zlib (GCC only), zlib-ng, ggml, fribidi, flatbuffers, SPIRV-Tools,
nlohmann-json and nanobind (scikit-build-core, for the image's two uv Pythons), and
bazel-flatc (Bazel native and opt, run-bazel.sh then run-buildlog.sh) and halide-apps
(`case-studies/halide/apps/baseline/linux`: the HelloWasm and HelloAndroid CMake builds).
libHalide's Linux baseline is the 7b one (`case-studies/halide/baseline/cmake-linux-gcc-release.jsonl`),
recorded by Halide's own Docker harness. The scripts take the compiler from `BASELINE_FAMILY`, use
ELF's `readelf` and `nm -D` where macOS has `otool` and `nm -gU`, and write
the same normalized files. The phase tests pick the baseline by host
(`tests/host.py: baseline_dir`), so the action-by-action comparisons run on
Linux too. Each study's README has a "Linux" section with its deviations.

The submodules' git directories live in the host repository, outside the
worktree copy, so `run.sh` mounts it read-only (`/host-git`) and points the
copies' `.git` files at it; the baseline scripts check their pins and
SPIRV-Tools' `build-version.inc` asks git.

The case studies that run on Linux: zlib, nlohmann-json, zlib-ng, ggml
(linked build and, on Clang, the arm64 ISA variants), fribidi, flatbuffers,
SPIRV-Tools, nanobind (CPython 3.13 abi3 and 3.11, from uv) and, with GCC only,
Qt (`qt`: Qt 6.11.3, the macOS study's version, from aqt rather than Ubuntu's 6.4.2, which has
another layout; moc/uic/rcc as host tools; the CMake consumer too), bazel-flatc (native and opt
against Bazel 8.8.1; no cross variant: its target is x86_64-macos), HelloWasm (emsdk) and HelloAndroid
(the emulated x86_64 NDK; both ABIs), all with the macOS specs and a CMake comparison, and, with GCC only,
libHalide (LLVM 22; `cases gcc halide`, configure, build of
all 2194 edges, the 32-test sample, and the graph comparison with the 7b CMake
baseline, whose totals must equal the macOS ones; cold build of that slice 90 s, see
`case-studies/halide/README.md` for the conditions). The macOS-only tests
(x86_64 and universal2 slices, lipo, Rosetta) skip on Linux.
The apps need the Halide checkout in the image build context (`halide-src.tar`, made by `run.sh image`
from the submodule) and, for the baseline comparison, the apps' sources, which `cases.sh` and
`baselines.sh` copy from the mount.

## Results

Each run saves its summary and logs in `linux/raw/`. The Halide case study has its own, separate Docker harness in
[`case-studies/halide/docker/`](../case-studies/halide/docker/README.md).

## CUDA (stdgpu)

`linux/cuda/Dockerfile` adds the CUDA 13.4 toolkit (linux-sbsa, copied from
`nvidia/cuda:13.4.2-devel-ubuntu24.04`) to the base image; build it with
`docker build -f linux/cuda/Dockerfile -t buildlog-cuda:13.4 linux/cuda` and
use it with `IMAGE=buildlog-cuda:13.4`: the real-nvcc tests
(`tests/test_cuda_toolkit.py`) and the stdgpu study
(`linux/run.sh baselines gcc stdgpu`, `linux/run.sh cases gcc stdgpu`; GCC only,
not in the default lists). The image has no GPU: programs are built, not run.
