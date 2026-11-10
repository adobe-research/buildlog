<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Plugins

Tier-1 plugins (Python packages registered under the `buildlog.plugins` entry
point, with layers, probes and providers). This file covers only the ones added
for the cross-compilation case studies; the others will be added later.

| plugin | what it is | lines (Datalog / Python, non-blank non-comment) |
|---|---|---|
| `buildlog-ndk` | The Android NDK: discovery, `ndk` toolchain, Android platforms, NDK default arguments | 42 / 104 |
| `buildlog-emsdk` | Emscripten: discovery, `emscripten` toolchain family, wasm32 platform, `.js`+`.wasm` programs | 50 / 73 |
| `buildlog-cuda` | NVIDIA CUDA (nvcc, linux): toolkit probe, host compiler, GPU architecture sets, RDC and the device link, the runtime as packages (`plugins/buildlog-cuda/README.md`) | 262 / 156 |

Both are **provisional**, just enough for `case-studies/halide/apps/`, and stand
in for the toolchain knowledge each ecosystem would ship (as CMake has
`android.toolchain.cmake` and `Emscripten.cmake`). They depend on `buildlog-cpp`
for the generic hooks: `cross_target` (a toolchain that targets only platforms
of its own), `platform_os_level`, `elf_driver`, and the capability probes, which
read any plugin's `res_cross_<name>_*` inventory rows beside the host inventory.
buildlog-emsdk also owns the `emscripten.simd` / `emscripten.threads` settings,
the link-time `-O3`, `-matomics`, the SDL2 port package (`emscripten::sdl2`) and the
configuration's CPU features; buildlog-ndk the system-library packages
(`ndk::log`, `ndk::android`, `ndk::atomic`).
Nothing project-specific lives in them; the Halide rules stay in the ordinary
rule package `case-studies/halide/packages`.

The Halide case study's former plugin `buildlog-llvm` (a provider reading
`LLVMExports.cmake`, 139 lines of Python and TOML) is gone: LLVM, Clang and LLD
are tier-2 packages, hand-written manifests that import the install's own CMake
exports and ship a few facts and rules (`case-studies/halide/llvm-install`).
