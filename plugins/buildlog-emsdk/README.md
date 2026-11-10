<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# buildlog-emsdk

Emscripten as a buildlog plugin. **Provisional: just enough for the Halide
HelloWasm case study, and a stand-in for what the Emscripten ecosystem would
ship** (the way `Emscripten.cmake` ships with emsdk for CMake). It knows
nothing about any project.

- Discovery: `EMSDK` (`emsdk.py`, `upstream/emscripten`); no variable, no platform.
- Toolchain family `emscripten`: `em++`/`emcc`/`emar`. It targets wasm32 by
  itself (no `--target`), takes clang's settings, and links with wasm-ld
  (archive cycles need no group).
- Platform `wasm32-emscripten` (`os=emscripten`, `cpu=wasm32`).
- Program format: an executable is `<name>.js` and the link also writes
  `<name>.wasm` (core's `platform_executable_suffix`, `platform_link_side_file`).
- `cross_target`, so that only this toolchain is a candidate for the platform.

- Settings `emscripten.simd` (`off|simd128`) and `emscripten.threads`
  (`off|threads`), default `off` (left out of configuration names), so a project
  states a wasm configuration as the platform plus settings. They give the open
  `config_cpu_feature` rows `simd128` / `threads` (rules that map them to a
  tool's own names, such as Halide's target features, read those) and, for
  threads, `-matomics` at link (`Threads::Threads` itself adds nothing on
  Emscripten).
- `-O3` at link for `native.opt = release`: emcc optimizes at link time.
- Ports as packages: `emscripten::sdl2` gives `--use-port=sdl2` at compile and
  link to whatever depends on it.
- Not spelled: `-sWASM=1` (emcc's default). A project's own options
  (`-sTOTAL_MEMORY=...`) stay in the project.

Binaryen: emcc uses `EM_BINARYEN_ROOT` if set.

Use: `uses = ["buildlog-cpp", "buildlog-emsdk"]` in the project.
