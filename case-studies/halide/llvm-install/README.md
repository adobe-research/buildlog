<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# LLVM, Clang and LLD as tier-2 packages

These files stand in for what LLVM upstream (or a distro) would install:
`<prefix>/share/buildlog/<Pkg>/manifest.toml` beside `lib/cmake/{llvm,clang,lld}`. Each manifest
`imports` its upstream's own CMake export (`LLVMExports.cmake`, `ClangTargets.cmake`,
`LLDTargets.cmake`) and ships a few facts (`facts.toml`: version, `LLVM_DEFINITIONS`, include dir,
RTTI/EH, `LLVM_TARGETS_TO_BUILD`) and a shared Datalog layer (`rules/`). One manifest per package, as
upstream has one `*Config.cmake` each; Clang and LLD `require` the LLVM version they were built with.
Nothing converts anything: the facts are LLVM's variables written as facts, the targets are the export
file's.

The overlay prefixes put the manifests beside an existing install without writing into it:
`lib`, `include` and `bin` are symlinks to the real prefix, `share/buildlog` is ours.

| overlay | beside | packages |
|---|---|---|
| `homebrew-llvm-22` | `/opt/homebrew/opt/llvm@22` (22.1.8) | LLVM, Clang |
| `homebrew-lld-22` | `/opt/homebrew/opt/lld@22` | LLD |
| `homebrew-llvm-21`, `homebrew-lld-21` | llvm@21, lld@21 (21.1.8) | the same, LLVM 21's facts |
| `linux-22` | `/usr/lib/llvm-22` (the Linux image) | LLVM, Clang, LLD |

A user preset binds one: `package_search_dir = [["buildlog", "<overlay>/share/buildlog"]]`
(`libhalide/presets/*.toml`). Two overlays for one package are core's `ambiguous package` error
until one is pinned. The overlay paths appear in the build graph where the real prefix would.

Size (non-blank, non-comment lines): rules `LLVM.dl` 31, `Clang.dl` 2, `LLD.dl` 4; for one macOS
overlay pair, manifests 39 + 16 and facts 32 + 2. The removed plugin `buildlog-llvm` was 127 lines of
Python, 8 of manifest and 4 of `__init__`.

## Cross builds (`cross-22`)

Rule packages load once per name, so a build with a macOS and a Linux LLVM cannot overlay
both. `cross-22/share/buildlog/{LLVM,Clang,LLD}` carries per-platform rows instead
(`llvm_define_on`, `llvm_include_dir_on`, `llvm_target_on`, `lld_include_dir_on`); the
preset `libhalide/presets/cross-linux-aarch64.toml` selects it and gives each platform's
CMake exports with `package_file_on`.
