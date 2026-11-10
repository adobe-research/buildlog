<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Halide case study: Docker scenarios

This directory holds the environments and the CMake ground truth for two Linux scenarios.
Nothing here touches `src/`, `plugins/`, `packages/` or `../libhalide`.

- **7b, native Linux/GCC.** Configure, build and test entirely inside a Linux arm64 container.
  libHalide is built with GCC 13, the second toolchain family after Apple clang. The runtime
  is still compiled by LLVM 22's clang, as on macOS.
- **7c, cross build.** The build runs on macOS arm64. Host tools and generators are built for
  macOS and run there. The product (libHalide, the runtime, the generated libraries, the
  tests) targets `aarch64-linux-gnu`. It compiles and links against a sysroot exported from
  the 7b image, using Homebrew `llvm@22` clang and `lld@22`. The tests run in the container.

Both builds use the pin of `../baseline/run-cmake.sh`, Halide main `4e272cbfcc`, exported
with `git archive`. They use the same `scope.toml`, normalizer and 32-test sample as the
macOS baselines.

| file | what |
|---|---|
| `linux/Dockerfile` | the image, `buildlog-halide-linux:22` |
| `build-image.sh` | builds it. Records versions, id and size in `raw/image.txt` |
| `run-cmake-linux.sh` | 7b: `configure build dump test normalize metrics`, each step in the container |
| `sysroot.sh` | 7c: exports the image as a sysroot |
| `run-cmake-cross.sh` | 7c: `setup hello host configure build dump test normalize metrics` |
| `cross/toolchain.linux-aarch64-clang.cmake` | 7c toolchain file |
| `cross/host-twin.py`, `cross/pkg/*` | 7c workarounds (see the [ledger](#7c-cmake-effort-ledger)) |
| `cross/hello.cpp` | 7c toolchain smoke test |
| `fileapi-query.json` | the File API query, the same as `../baseline`'s |

The gitignored directories are:
- `raw/`: logs, the sparse image and scratch;
- `sysroot/`: the default `sysroot.sh` output;
- `xvol/`: the 7c mount point.

The raw build dumps go to `../baseline/raw/{linux-gcc-release,cross-linux-aarch64}/`, which is
also gitignored.

## Reproduce

To build the image and run 7b:

```sh
./build-image.sh                   # about 2 min
./run-cmake-linux.sh               # configure build dump test normalize; about 3.5 min
./run-cmake-linux.sh metrics
```

To run 7c:

```sh
./run-cmake-cross.sh               # setup hello host configure build dump test normalize
./run-cmake-cross.sh metrics
hdiutil detach case-studies/halide/docker/xvol    # when done; `setup` re-attaches
```

Prerequisites:
- Docker Desktop. It was 29.8 here, with a `linux/arm64` server, 16 CPUs and 25 GB.
- For 7c, the Homebrew `llvm@22`, `lld@22`, `flatbuffers` 25.12.19, `cmake` and `ninja`, as for the macOS baseline.
- `HALIDE_SRC`: a Halide checkout whose git objects contain the pin. The default is the `case-studies/halide/upstream` submodule.

### Case-insensitive APFS breaks Linux-targeted Halide builds

Both scenarios keep the Halide source off the default APFS volume:
- 7b copies the source into the Docker volume `buildlog-halide-linux-src` and mounts it read-only at `/src`.
- 7c puts the source, sysroot and build trees on a case-sensitive APFS sparse image, `raw/cross.sparseimage`. It is attached with `-nobrowse` at `xvol/`, and grows to about 6.2 GB.

The reason: libHalide compiles with `-I$SRC/src`, and `src/GeneratorCache.cpp` includes `<elf.h>`.
On a case-insensitive file system that finds Halide's own `src/Elf.h` before glibc's `elf.h`, and the
compile fails. This happens through a Docker bind mount (7b's first attempt) and on macOS
(7c) alike. The macOS build never includes `<elf.h>`, so the problem does not show there.

## Pinned versions

**Image** `buildlog-halide-linux:22`:
- id `sha256:3887c009b1f560b8c8d6d56db0a13e51f9574227bcb35fd52ac9972a7d48543f`;
- 2.56 GB (2559862254 bytes);
- built in 103 s.

| component | version | source |
|---|---|---|
| base | Ubuntu 24.04, `ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3` | Docker Hub, native arm64 |
| GCC (libHalide's C/C++ compiler) | 13.3.0, the default `/usr/bin/cc`, `c++` | Ubuntu |
| LLVM, Clang, LLD | 22.1.8, package `1:22.1.8~++20260714014902+ca7933e47d3a-1~exp1~20260714135019.80` | apt.llvm.org `llvm-toolchain-noble-22`. The same 22.1.8 as Homebrew `llvm@22` |
| CMake, Ninja, uv | 4.4.3, 1.13.2, 0.12.19 | PyPI. The same as the macOS baseline |
| flatbuffers, flatc | 25.12.19 | built from source (tag `v25.12.19`). Must equal Homebrew's for 7c, see below |
| wabt | 1.0.42 | built from source (`WITH_WASI=ON`, PIC), so the default `Halide_WASM_BACKEND=wabt` works |
| glibc, libstdc++ | 2.39 (`2.39-0ubuntu8.9`), GCC 13 (`__GLIBCXX__=20240904`) | Ubuntu |
| libpng, libjpeg-turbo | 1.6.43, 2.1.5 | Ubuntu |
| Python, pybind11 | 3.12.3, 2.11.1 | Ubuntu. `python3-dev`/`pybind11-dev` are needed because `WITH_PYTHON_BINDINGS` defaults to ON, and `li2018` needs Python `Development.Module` |

**Sysroot.** `sysroot.sh` produces 1.5 GB, 13882 files:
- `usr/include`, `usr/lib/aarch64-linux-gnu`, `usr/lib/gcc`, `usr/lib/llvm-22`, `usr/lib/cmake`;
- `usr/local/{include,lib,bin,share}` and `/lib`.

Extraction takes 22 s to the normal disk and took 943 s to the sparse image, under heavy machine load.

**7c host tools** (Homebrew):
- `llvm@22` 22.1.8: clang, clang-22, llvm-as, llvm-ar and llvm-ranlib;
- `lld@22`;
- flatc 25.12.19;
- CMake 4.4.3 and Ninja 1.13.2.

## 7b result

- **Configure:** `-G Ninja -DCMAKE_BUILD_TYPE=Release -DHalide_LLVM_ROOT=/usr/lib/llvm-22 -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`, with everything else at its default.
- **Build:** `ninja all -j16` takes 136 s (one run).
- **Tests:** of the 32 sampled tests, 30 pass and 2 are skipped: `correctness_multi_gpu_gpu_multi_device` and `correctness_gpu_allocation_cache` skip, as on macOS (no GPU).
- **Baseline:** `../baseline/cmake-linux-gcc-release.jsonl`. See `../baseline/metrics.md` and `../baseline/scope.md`.

## 7c result

**It works.** Two build trees and two configure invocations build the complete Linux product. The
sampled tests pass in the container: 30 of the 32 pass, and the same two GPU tests are skipped.

| step | wall time (single run, unless noted) |
|---|---:|
| hello-world cross compile, then run in the container | ok: iostream, exceptions, `std::thread` and `dlopen` all work |
| tree 1, host: configure (`--preset macOS`) | 6.4 s (5.5–5.8 s cold in `metrics`) |
| tree 1, host: build the 8 tool/generator/plugin targets | 44 s |
| tree 2, target: configure | 4.9 s (4.9 s cold, 2.7–3.0 s warm) |
| tree 2, target: build `all` | 87 s (85 s in a previous clean run) |
| tree 2: `ninja` no-op | 0.09 s |
| ctest sample in the container | 66–68 s (two runs) |

- **Machine load.** The machine was heavily loaded throughout (load average 20–40, from Spotlight, XProtect and a
  concurrent agent), so treat the times as rough.
  - Two earlier builds of the same graph took about 1000 s each.
  - On a quiet machine, 16 parallel compiles of libHalide sources take 8 s. The same compiles
    took 191 s under load.
- **Generator targets.** The generators decide the Linux target from Halide's `cmake` target. In the target
  tree `Halide_HOST_TARGET=arm-64-macos` (from `CMAKE_HOST_SYSTEM`), but `Halide_CMAKE_TARGET=arm-64-linux` (from the
  toolchain's `CMAKE_SYSTEM_*`). `Halide_TARGET` defaults to `cmake`, so every
  `add_halide_library` passes `target=arm-64-linux[-features]`, e.g.
  `target=arm-64-linux-no_runtime` 89 times.
  - Features keyed per target (`FEATURES[arm-64-macos] arm_dot_prod-arm_fp16` in adams2019) simply drop out.
  - No `Halide_TARGET` or `TARGETS` override was needed.

### 7c CMake effort ledger

**Trees and invocations.**
- 2 build trees and 2 configure invocations.
- 1 partial build of tree 1 before tree 2 can build. It covers:
  - `Halide-halide_generators` (all 55 exported generators);
  - `binary2cpp`, `build_halide_h`, `regexp_replace`;
  - `Halide_Adams2019`, `Halide_Anderson2021`, `Halide_Li2018`, `Halide_Mullapudi2016`.
- The build is followed by a `docker run` for ctest.

**Tree 2 cache variables** (`raw/cross-linux-aarch64/configure-args.txt`). Beyond the 7b flags, tree 2 needs 3 standard ones and 6 workarounds (7 before the pin moved to `4e272cbfcc`, which has #9548).

| # | flag | standard or workaround | why |
|---|---|---|---|
| 1 | `CMAKE_TOOLCHAIN_FILE=cross/toolchain.linux-aarch64-clang.cmake` | standard | Linux/aarch64, Homebrew clang with `CMAKE_<LANG>_COMPILER_TARGET`, llvm-ar/ranlib, `--ld-path=` lld@22 (llvm@22 ships no lld), find-root modes PROGRAM=NEVER and the rest ONLY |
| 2 | `CMAKE_SYSROOT=xvol/sysroot` | standard | on the command line so try_compile inherits it |
| 3 | `CMAKE_EXPORT_COMPILE_COMMANDS=ON` | standard | as in every baseline |
| 4 | `CMAKE_FIND_ROOT_PATH=$SRC/dependencies` | workaround | Halide's `find_package(VulkanHeaders/SPIRV-Headers HINTS ${Halide_SOURCE_DIR}/dependencies/…)` gets re-rooted into the sysroot and fails |
| 5 | `WITH_PYTHON_BINDINGS=OFF` | workaround | `find_package(Python Development.Module)` mixes the host interpreter (3.10) with the sysroot, and li2018's bindings cannot be configured. Out of scope anyway |
| 6 | `WITH_TUTORIALS=OFF` | workaround | the `lesson_*_generate` tools have no host twin, since tree 1 does not build tutorials. Out of scope anyway |
| 7 | `CMAKE_CROSSCOMPILING_EMULATOR=cross/host-twin.py;--sysroot;…;--` | hack | see below |
| 8 | `FlatBuffers_DIR=cross/pkg/flatbuffers` | hack | shadow package: sysroot library targets and Homebrew's `FlatcTargets.cmake` |
| 9 | `Halide-halide_generators_DIR=cross/pkg/Halide-halide_generators` + `HALIDE_CROSS_HOST_BUILD=<tree 1>` | hack, wrapping the supported `Halide-halide_generators_ROOT` | shadow package: imports tree 1's generators and rewrites each `IMPORTED_LOCATION` to a wrapper script through host-twin.py |
| 10 | *(removed at pin `4e272cbfcc`)* `CMAKE_PROJECT_Halide_INCLUDE=cross/halide-project-include.cmake` | was a workaround, for Halide#9480 | forced `-include sys/auxv.h` on `src/Target.cpp`; fixed upstream in #9548, workaround removed at pin `4e272cbfcc` |

LLVM needs no flag. `FindHalide_LLVM`'s built-in search path `/usr/lib/llvm-22` is re-rooted into the
sysroot and finds the Linux LLVM (`LLVM_DIR=$SYSROOT/usr/lib/llvm-22/cmake`, `Halide_LLVM_SHARED_LIBS`
is on because apt's LLVM is the dylib build).

**Why an "emulator" that runs host twins instead of qemu or docker.** Halide's documented cross
flow (`doc/HalideCMakePackage.md`) builds generators in a host tree and imports them. Many other
build-time executables are still *in-tree targets* of the Linux tree:
- `binary2cpp` for the adams2019 weights;
- `build_halide_h`;
- `regexp_replace`;
- the autoscheduler `*_cost_model.generator`s.

Halide's runtime commands also run `$<TARGET_FILE:clang>` and `llvm-as`, which come from the
**Linux** LLVM package in the sysroot. CMake without an emulator leaves `binary2cpp` as a
bare name ("command not found"), and it runs the Linux clang directly ("cannot execute
binary file", 408 runtime compiles).

`host-twin.py` is plugged in as `CMAKE_CROSSCOMPILING_EMULATOR`. It maps:
- sysroot executables to the same-named Homebrew tool;
- `$BUILD/…` executables to `<tree 1>/…`;
- `-p` autoscheduler plugins likewise.

On Linux (ctest in the container) it execs its argument unchanged. It is prefixed to 1303 in-scope edges.

**CMake gaps this exposed** (each checked in isolation):
1. **`CMAKE_CROSSCOMPILING_EMULATOR` is not applied to IMPORTED executables.** A minimal test project (not kept) confirmed this.
   - Imported generators given `AUTOSCHEDULER Halide::Adams2019` receive `-p <Linux libautoschedule_adams2019.so>`, and the macOS generator's `dlopen` fails ("not valid mach-o file").
   - Hence shadow package 9, which puts the wrapper script in `IMPORTED_LOCATION`.
2. **One package namespace per build.** The target needs the sysroot's `flatbuffers::flatbuffers` and a host `flatbuffers::flatc`. The sysroot's Linux flatc fails with exit code 126. Hence shadow package 8.
   - The two flatbuffers versions must be equal, because the generated header `static_assert`s the version.
3. **No cross-tree dependency.** Tree 2's edges do not depend on tree 1's outputs; the twins are hidden inside the emulator's argv.
   - Editing a generator and rebuilding only tree 2 silently runs the stale host twin.
   - The user must rebuild tree 1 first, by hand or by script.
4. **Dead work.** Tree 2 still builds Linux `binary2cpp`, `build_halide_h`, `regexp_replace` and both `*_cost_model.generator`s. They are never run; only their twins are.
   - The Linux autoscheduler plugins *are* needed, but only at test time.
5. **Classification.** The 408 runtime `clang-22`/`llvm-as` edges start with the emulator, so the normalizer classifies them as `custom` rather than `compile`. This is the only kind change against 7b.

**Environment surprises** (not CMake's fault, but they cost time):
- **Case-insensitive APFS.** See above.
- **Halide bug (history; fixed upstream in #9548, workaround removed at pin `4e272cbfcc`).** `src/runtime/auxv_ops.h` declares `extern "C" unsigned long getauxval(unsigned long)` without `noexcept`. Target.cpp includes it before glibc's `__THROW` declaration.
  - GCC accepts the redeclaration. Clang rejects it on *any* aarch64 Linux; this was checked natively in the container with `clang++-22`.
  - So 7b (GCC) never sees it, and a native clang Linux build would.
  - Reported as #9480 and fixed by #9548, which moves the `runtime/*` includes after `<sys/auxv.h>`.
- **usrmerge in a sysroot.** With Ubuntu's `lib -> usr/lib` symlink, clang selects the GCC installation `$SYSROOT/lib/gcc/aarch64-linux-gnu/13`. It then adds `…/13/../../../../include/c++/13`, which it records in depfiles *lexically collapsed* as `$SYSROOT/include/c++/13/…`.
  - That path does not exist, so every edge that includes a libstdc++ header stays dirty forever: 3296 edges, a 66 s "no-op".
  - `sysroot.sh` therefore makes `lib/` a real directory of links to each `usr/lib` entry except `gcc`. Clang then selects `usr/lib/gcc`, and the no-op takes 0.09 s.
- **C++20 module scanning.** Only the cross tree gets it. wabt 1.0.42's exported target carries `cxx_std_20` (scope.md oddity 16), so libHalide compiles as C++20 in every variant.
  - CMake scans C++20 sources only with a compiler that supports P1689, such as clang ≥ 16.
  - Apple clang and GCC 13 are not such compilers, so only the cross tree gets 194 `clang-scan-deps` `.ddi` edges plus a `CXX.dd` collation edge for libHalide.

**The failed iterations** (logs in the gitignored `raw/cross/try*-{configure,build}.log`), in order:

| attempt | result | fix added |
|---|---|---|
| try0 | configure fails: VulkanHeaders not found | 4 |
| try1 | configure fails: Python Development.Module | 5; also `_ROOT` for the generators |
| try2 | configures. The build fails: 408× Linux clang-22, 15× llvm-as, `binary2cpp: command not found`, build_halide_h, flatc 126, adams/anderson `baseline.cpp` | 7, 8 |
| try3/4 | 1 failure: Target.cpp (`getauxval`) | 10 |
| try5 | 27 failures: imported generators `dlopen` Linux plugins; tutorial generators have no twin | 9, 6 |
| try6 | builds cleanly | — |

Tree 1's target list also came from these failures. Scripted, the flow is `run-cmake-cross.sh` (238 lines). The toolchain file, `host-twin.py` and the two CMake shims add another 162 lines, 400 in all (420 before the #9548 workaround was removed).

## buildlog on 7c

buildlog builds and tests the same cross product in one tree with one upstream workaround:
30 passed, 2 skipped. See "Phase 7c on buildlog" in [../README.md](../README.md) and
`run-buildlog-cross.sh`.
