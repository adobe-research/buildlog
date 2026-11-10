<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Halide CMake baseline — metrics

These numbers are for Halide at `35b7aa604e` plus PR #9473 (`73009e8525`, the wabt 1.0.42
fix), the pin before the repin to main `f370931c79` (whose graphs are identical, and the pin is now `4e272cbfcc`, whose graphs are not: see README, "Repin to 4e272cbfcc"; its release build
took 116 s, a single run), `macOS` preset (Release, Ninja), `-DHalide_LLVM_ROOT=$LLVM` (Homebrew `llvm@22`, 22.1.8),
everything else default, including `Halide_WASM_BACKEND=wabt` (see [../README.md](../README.md)). Raw values are in the gitignored `raw/release/metrics.txt`.
To regenerate them, run `./run-cmake.sh release metrics`.

## Machine

| item | value |
|---|---|
| CPU | Apple M3 Max, 16 cores (12 performance + 4 efficiency), 48 GB RAM |
| OS | macOS 26.5.2 (25F84) |
| Xcode | 26.6 (17F113); Apple clang 21.0.0 (clang-2100.1.1.101) is the C/C++ compiler |
| LLVM for Halide | Homebrew `llvm@22` 22.1.8 (with `lld@22`); its `clang-22`/`llvm-as` compile the runtime |
| CMake / Ninja | 4.4.3 / 1.13.2 |
| other | flatc 25.12.19, uv 0.12.19 |

## Timings

| measurement | runs (s) | median (s) |
|---|---|---:|
| cold configure: empty build dir, `cmake --preset macOS …` (configure + generate) | 5.990, 5.914, 6.005 | **5.99** |
| warm configure: rerun, no changes | 3.770, 3.530, 3.610 | **3.61** |
| `ninja` no-op after full build | 0.157, 0.072, 0.070 | **0.072** |

The first no-op run includes a cold `.ninja_log`/`.ninja_deps` read. The median ignores it.

**Full builds** were each run once, with `ninja -j16` on the full default `all`
target (9092 edges in `build.ninja`, which includes about 640 correctness tests):

| variant | wall time | notes |
|---|---:|---|
| Release | **113 s** | clean, from empty build dir |
| Debug | 134 s | clean |
| ASan | 125 s | clean, `ninja -k 0 all`: everything except the 16 python_bindings edges that fail |
| x86_64 (cross) | 40 s until failure | libHalide link fails: Homebrew LLVM, lld, wabt and OpenSSL are arm64-only |
| universal2 (PR #9469 + #9473; in main since) | 74 s until failure | same link failure |

The first baseline, with `Halide_WASM_BACKEND=OFF`, took 100 s (Release) and 128 s (Debug).
I did not measure how much of the +13 s (Release) comes from the executor. With
`WITH_WABT`, `WasmExecutor.cpp` compiles its wabt half and libHalide links `libwabt.a`, but
both runs are single samples.

Each time is a single sample on a machine that was otherwise mostly idle. Treat them as ±10%.

## Size of the CMake build description (in scope)

`metrics.py cmake-size` counts non-blank, non-comment lines in the CMake files that contribute
to in-scope targets. The upper bound is every project file that the File API
`cmakeFiles` reply lists as read at configure time.

**Reusable helper modules** (installed with Halide and used by downstream projects): 7 files, 1585 lines

| file | lines |
|---|---:|
| `cmake/FindHalide_LLVM.cmake` | 122 |
| `cmake/HalideGeneratorHelpers.cmake` | 1118 |
| `cmake/HalideTargetHelpers.cmake` | 75 |
| `cmake/TargetExportScript.cmake` | 39 |
| `packaging/HalideAutoschedulersConfig.cmake` | 3 |
| `packaging/HalideCompilerConfig.cmake` | 140 |
| `packaging/HalideConfig.cmake` | 88 |

**Project-internal modules** (not installed): 4 files, 234 lines

| file | lines |
|---|---:|
| `cmake/HalideFeatures.cmake` | 49 |
| `cmake/HalidePackageConfigHelpers.cmake` | 56 |
| `cmake/HalideTestHelpers.cmake` | 112 |
| `cmake/WipeStandardFlags.cmake` | 17 |

**Project `CMakeLists.txt` files**: 16 files, 3179 lines

| file | lines |
|---|---:|
| `CMakeLists.txt` | 175 |
| `packaging/CMakeLists.txt` | 294 |
| `src/CMakeLists.txt` | 562 |
| `src/autoschedulers/CMakeLists.txt` | 25 |
| `src/autoschedulers/adams2019/CMakeLists.txt` | 69 |
| `src/autoschedulers/anderson2021/CMakeLists.txt` | 77 |
| `src/autoschedulers/common/CMakeLists.txt` | 17 |
| `src/autoschedulers/li2018/CMakeLists.txt` | 1 |
| `src/autoschedulers/mullapudi2016/CMakeLists.txt` | 1 |
| `src/runtime/CMakeLists.txt` | 325 |
| `test/CMakeLists.txt` | 51 |
| `test/autoschedulers/CMakeLists.txt` | 9 |
| `test/autoschedulers/adams2019/CMakeLists.txt` | 71 |
| `test/correctness/CMakeLists.txt` | 718 |
| `test/generator/CMakeLists.txt` | 719 |
| `tools/CMakeLists.txt` | 65 |

- **Total, Halide-authored: 4998 lines in 27 files.** 1585 of those lines are reusable helpers and 3413 are project-specific.
  - An earlier version of this file said 4918 lines in 25 files. That number was stale: it predates
    adding `adams2019_demo_apps_autoscheduler` to the test sample, which brings in
    `test/autoschedulers/CMakeLists.txt` and `test/autoschedulers/adams2019/CMakeLists.txt`.
    Enabling wabt changes no file counts.
- Excluded: vendored SPIRV/Vulkan package configs under `dependencies/` (5 files, 250 lines).
- **Upper bound** (all project CMake read at configure): 6901 lines in 64 files.
  This adds python_bindings, apps and util lists, and so on.
- Not counted: CMake's own modules (FindPNG, FindJPEG, FindOpenSSL, …) and the LLVM/Clang/LLD/flatbuffers/wabt package configs.
  They are part of the implicit behaviour below, not of Halide's description.

## Implicit choices CMake makes (need a pin or derivation in buildlog)

This section comes from `metrics.py implicit`, which reads the File API `cache` reply and the configure log.
Paths use the normalizer placeholders.

**Toolchain detection**
- C/C++ compiler: `/usr/bin/cc` and `/usr/bin/c++`, identified as AppleClang 21.0.0.21000101. This is a first-on-PATH choice through the Xcode shims.
- `ar`, `ranlib` and `ld` from `/usr/bin`.
- `CMAKE_OSX_SYSROOT` and `CMAKE_OSX_DEPLOYMENT_TARGET` are empty.
  - The SDK (`$SDK`) is therefore whatever `xcrun` picks.
  - No `-mmacosx-version-min` is passed.
- Host platform triple `arm-64-macos`; the `cmake` AOT target resolves to the same.
  - The generator `TARGETS cmake` expansion depends on this.

**LLVM selection**
- `Halide_LLVM_ROOT=$LLVM` is given explicitly. `FindHalide_LLVM` then finds `LLVM_DIR=$LLVM/lib/cmake/llvm` at version 22.1.8.
- The accepted range is `21...99`, so without the override Homebrew's default `llvm` keg (23.1.2) would be accepted just as happily.
- Found components: WebAssembly X86 AArch64 ARM Hexagon NVPTX PowerPC RISCV.
- `Clang_DIR=$LLVM/lib/cmake/clang`.
- `LLD_DIR=$HOMEBREW/opt/lld@22/lib/cmake/lld` comes from a separate Homebrew keg, found through a search path rather than from `Halide_LLVM_ROOT`.
- LLVM's own exported dependencies:
  - FFI (`$SDK/usr/lib/libffi.tbd`);
  - LibEdit 2.11 (`$SDK`);
  - ZLIB 1.2.12 (`$SDK/usr/lib/libz.tbd`);
  - zstd (`$HOMEBREW/lib/libzstd.dylib`);
  - LibXml2 2.9.13 (`$SDK`).

**`find_package` first-match results**
- `FlatBuffers_DIR=$HOMEBREW/lib/cmake/flatbuffers`. This one decides WITH_SERIALIZATION's `flatc` and headers.
- `wabt_DIR=$HOMEBREW/lib/cmake/wabt` (wabt 1.0.42) for the default `Halide_WASM_BACKEND=wabt`.
  - Its config does `find_dependency(OpenSSL)`, which finds OpenSSL 3.6.4 at the *versioned* Cellar path:
    `OPENSSL_CRYPTO_LIBRARY=$HOMEBREW/Cellar/openssl@3/3.6.4/lib/libcrypto.dylib` and
    `OPENSSL_INCLUDE_DIR=$HOMEBREW/Cellar/openssl@3/3.6.4/include`.
  - Its imported target carries `cxx_std_20`, which raises libHalide to `-std=c++20` (scope.md, oddity 16).
- JPEG 80 (`$HOMEBREW/lib/libjpeg.dylib`) and PNG 1.6.58 (`$HOMEBREW/lib/libpng.dylib`) are used by `Halide::ImageIO` in tests and tools.
  - The `*_LIBRARY_DEBUG` entries are NOTFOUND.
- `SPIRV-Headers_DIR` and `VulkanHeaders_DIR` point at the vendored copies under `$SRC/dependencies`.
- Python 3.10.19 at `$HOME/.local/bin/python3.10` (the highest 3.x ≥ 3.10 found first) and pybind11 3.1.0 from `$HOMEBREW`.
  - Both are only for python_bindings, which are out of scope.
- `pkg-config`, `gdb` and `lldb` are found, but no in-scope edge uses them.
- `Halide-halide_generators_DIR` etc. are NOTFOUND. The generators are therefore built in-tree rather than imported.
  - This is the one-line switch between "host tool built in the graph" and "imported".

**Configure-time probes that change the graph**
- `LINKER_HAS_FLAG_VERSION_SCRIPT` fails and `LINKER_HAS_FLAG_EXPORTED_SYMBOLS_LIST` succeeds. That selects `-exported_symbols_list` for libHalide.
- `HALIDE_INTERNAL_USING_ASAN` is not found in Release. In the ASan variant it is found, and that toggles ASan-aware code paths.
- `HAVE_LIBFUZZER_FLAGS` fails (no `-fsanitize=fuzzer` in the flags), so `test/fuzz` links Halide's standalone
  `halide_fuzz_main.cpp` driver instead of libFuzzer. Fuzz tests are out of scope.
- `HAS_FLTO_THIN` and `HAS_FLTO` succeed.
- `CMAKE_HAVE_LIBC_PTHREAD` succeeds (Threads is in libc).
- `HAVE_FFI_CALL` succeeds.

The full table is `raw/release/implicit.md` (gitignored, regenerated by the `metrics` step).

## Linux scenarios (M3 phases 7b and 7c)

These were recorded by `../docker/run-cmake-linux.sh` and `../docker/run-cmake-cross.sh`. The
image, versions and the 7c effort ledger are in [../docker/README.md](../docker/README.md). Raw
values are in `raw/{linux-gcc-release,cross-linux-aarch64}/`: `metrics.txt`, `*-time.txt`,
`ctest.log` and `implicit.md`.

**Environments**

| | 7b linux-gcc-release | 7c cross-linux-aarch64 |
|---|---|---|
| where it runs | Docker Desktop 29.8 Linux VM on the Mac above (16 CPUs, 25 GB), native arm64 | macOS, as above. Tests run in the container |
| C/C++ compiler | GCC 13.3.0 (`/usr/bin/cc`, `c++`) | Homebrew clang 22.1.8, `--target=aarch64-linux-gnu --sysroot=$SYSROOT` |
| linker, archiver | GNU ld and `ar` (binutils 2.42) | lld 22 (`lld@22`), llvm-ar/llvm-ranlib |
| LLVM for Halide | apt.llvm.org 22.1.8 at `/usr/lib/llvm-22` (dylib build) | the same, from the sysroot. The runtime is built by host twins: Homebrew `clang-22`/`llvm-as` |
| libc, libstdc++ | glibc 2.39, libstdc++ 13 | the same, from the sysroot |
| CMake, Ninja | 4.4.3, 1.13.2 (PyPI) | 4.4.3, 1.13.2 (Homebrew) |
| build trees, configure invocations | 1, 1 | 2, 2 (macOS host tree + Linux target tree) |

**Timings** are single runs unless the row lists three. The Mac was heavily loaded during the 7c runs
(load average 20–40 from Spotlight, XProtect and a concurrent agent), so treat those as
rough. The 7b container timings exclude `docker run` startup.

| measurement | 7b runs (s) | 7b median | 7c runs (s) | 7c median |
|---|---|---:|---|---:|
| cold configure | 2.648, 2.530, 2.663 | **2.65** | 4.946, 4.853, 4.979 (target tree) | **4.95** |
| warm configure | 1.791, 1.747, 1.745 | **1.75** | 5.594, 3.004, 2.719 | **3.00** |
| `ninja` no-op | 0.060, 0.056, 0.058 | **0.058** | 0.108, 0.087, 0.088 | **0.088** |
| host tree cold configure (`--preset macOS`) | — | | 5.575, 5.519, 5.546 (first pass); 5.846, 5.812, 5.848 (second) | **5.6–5.8** |
| host tree build (8 tool, generator and plugin targets) | — | | 44 | |
| full build, `ninja -j16 all` | 136 | | 87 and 85 (two clean runs) | |
| ctest sample (32 tests, `-j16`) | 60.2 (30 pass, 2 skipped) | | 65.7–68.5 in the container (30 pass, 2 skipped) | |

- **Test times.** `correctness_cross_compilation` alone takes about 48–50 s in both scenarios.
- **Build times.** The two 7c full builds are much faster than the 7b one, although both compile the same
  sources: clang is faster than GCC 13 on libHalide, and the container adds VM overhead.
  - Neither effect was measured separately.
  - Two earlier 7c builds under even heavier load took about 1000 s each. On an idle machine,
    16 parallel libHalide compiles take 8 s; under that load they took 191 s.
- **7c no-op.** Before `sysroot.sh` undid the usrmerge `lib` link, the 7c "no-op" rebuilt 3296 edges (66 s)
  on every run (README, "usrmerge in a sysroot").

**Implicit choices that differ from macOS.** The full tables are in `raw/*/implicit.md`.

7b (linux-gcc-release):
- **Compiler.** `/usr/bin/cc`/`c++` is GNU 13.3.0, and `CMAKE_<LANG>_COMPILER_AR`/`RANLIB` are `gcc-ar-13`/`gcc-ranlib-13`.
  - `Halide_TARGET` resolves to `host` = `arm-64-linux`.
- **Probes.** `LINKER_HAS_FLAG_VERSION_SCRIPT` succeeds, so libHalide is linked with `-Wl,--version-script`
  instead of `-exported_symbols_list`.
  - `HAS_FLTO` succeeds, but no thin-LTO probe runs.
- **Packages.** Every `find_package` resolves under `/usr` or `/usr/local`: FlatBuffers and wabt from
  `/usr/local/lib/cmake`, OpenSSL 3.0.13, PNG 1.6.43, JPEG 80, ZLIB 1.3, LibXml2 2.9.14 and
  CURL 8.5.0 from `/usr/lib/aarch64-linux-gnu`.
  - Python 3.12.3 and pybind11 2.11.1 are found, and python_bindings configure.
- **wabt.** wabt's `cxx_std_20` raises libHalide to `-std=c++20` here too (oddity 16). GCC 13 is not a
  compiler CMake scans modules with, so the graph gets no `.ddi` edges.

7c (cross-linux-aarch64):
- **LLVM.** It is found without a hint: `FindHalide_LLVM`'s built-in `/usr/lib/llvm-22` is re-rooted into the sysroot.
- **Packages.** Every library and header package is found in `$SYSROOT` (find-root mode ONLY). The exceptions are the shadowed `flatc` (Homebrew) and the imported generators (host tree).
- **Targets.** `Halide_HOST_TARGET=arm-64-macos` and `Halide_CMAKE_TARGET=arm-64-linux`. `Halide_TARGET=cmake`, so the
  generators emit `target=arm-64-linux…`.
- **Module scanning.** Clang 22 supports P1689, so libHalide's C++20 compiles get CMake's module-scanning
  edges: 194 `.ddi` plus `CXX.dd`.
