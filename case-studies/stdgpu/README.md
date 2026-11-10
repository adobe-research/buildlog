<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Case study: building stdgpu (CUDA) with buildlog

stdgpu 1.3.0 (`stotko/stdgpu` at 503c63af) is a C++17 library of GPU
containers (`vector`, `unordered_map`, `deque`, ...) over Thrust. It is the
first CUDA study: a static library of host C++ sources, and twelve example
programs, nine of them `.cu` files nvcc compiles. This study builds it with
buildlog-cuda and compares the compile and link commands with stdgpu's own CMake
build. **Compile and link only**: the programs need a GPU, and this machine has
none (running them is deferred to a Linux machine with one).

## Layout

```
case-studies/stdgpu/
  README.md          this file
  upstream/          submodule, pinned
  baseline/
    run-cmake.sh     CMake configure and build (Release, Ninja, CMAKE_CUDA_ARCHITECTURES=80)
    linux/           environment.txt, commands.txt (`ninja -t commands`), config.h, compare.txt
  project/           buildlog.toml, spec.toml, presets/gcc-ampere.toml
  compare.py         the comparison below
```

## Running it

The toolkit is the CUDA 13.4 one for linux-sbsa in the arm64 image
`linux/cuda/Dockerfile` (a Mac has no toolkit):

```
IMAGE=buildlog-cuda:13.4 linux/run.sh baselines gcc stdgpu   # the CMake baseline
IMAGE=buildlog-cuda:13.4 linux/run.sh cases gcc stdgpu       # configure, build, compare
```

The build is `arm64-linux.release.none.ampere`: GCC 13 is the one C++ toolchain, so
nvcc's host compiler is derived, not pinned; the architecture set `ampere` is
`sm_80` and `compute_80`, as `-DCMAKE_CUDA_ARCHITECTURES=80`; the runtime is
`cuda::cudart_static`, as `STDGPU_BUILD_SHARED_LIBS=OFF` has it. Thrust is the
toolkit's `cuda::thrust` package. The `#cmakedefine01` lines of `config.h.in`
(not in the configure-file dialect) are `#define` lines in the spec.

## Fidelity (`baseline/linux/compare.txt`)

All 17 compiles (five library sources, three C++ and nine CUDA example
sources) and 13 links (the archive and twelve programs) were compared with
`compare.py`.

- **Compiles: identical** as sets of defines, include directories, flags and
  `--generate-code` specs (`-Xcompiler=` lists are compared per warning). The
  nvcc and host warnings, `-O3 -DNDEBUG`, the thrust definition, the
  `config.h` directory and the toolkit's (`cccl` first) directories all agree.
- **Not compared, differs by design**: the order of flags (buildlog-cpp sorts a
  set of warnings), `-O3` twice (the configuration's and stdgpu's own), buildlog-cpp's
  `-isystem` for a dependency's include directories where CMake uses `-I`, and
  CMake's `-forward-unknown-to-host-compiler` (nvcc is given no unknown flags here).
- **Links**: the archive has identical inputs. Every program links the same
  objects, `libstdgpu.a` and `libcudart_static.a`. CMake's lines also have
  `-lcudadevrt` (CMake's implicit RDC runtime), a second `-lcudart_static`,
  `librt.a` (an absolute path from `CUDA::cudart_static_deps`) and the system
  libraries in the order `-lrt -lpthread -ldl`; buildlog has `-ldl -lpthread -lrt`.
  Neither line has a device link: stdgpu does not use RDC.

Both builds finish with the toolkit's nvcc 13.4 (31 commands for buildlog,
the 30 of CMake plus the `config.h` action).

## Open

- The programs are not run: no GPU here.
- Only GCC and one architecture are covered; `STDGPU_BUILD_SHARED_LIBS=ON`,
  the tests (googletest by `FetchContent`) and the benchmarks are out of scope.
