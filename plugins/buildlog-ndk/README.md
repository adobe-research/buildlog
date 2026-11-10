<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# buildlog-ndk

The Android NDK as a buildlog plugin. **Provisional: just enough for the
Halide HelloAndroid case study, and a stand-in for what the NDK ecosystem would
ship** (the way `android.toolchain.cmake` ships with the NDK for CMake). It
knows nothing about any project.

- Discovery: `ANDROID_NDK_ROOT` (`ndk.py`); no variable, no platforms.
- Toolchain `ndk`: the NDK's `clang`/`clang++`/`llvm-ar` (family `clang`).
- Platforms `arm64-android` and `x86_64-android` (`os=android`); the other ABIs
  are not offered. The API level is the project's: `platform_os_level` (buildlog-cpp),
  spelled into the triple (`--target=aarch64-linux-android23`).
- The NDK's default compile arguments (`--sysroot`, `-DANDROID`,
  `-fstack-protector-strong`, ...) and link arguments (`--no-undefined`,
  `--gc-sections`, static libc++), as `android.toolchain.cmake` passes them.
- ELF linking for Android (`elf_driver`), and `cross_target`, so that no host
  toolchain is a candidate for these platforms and the NDK is a candidate for
  nothing else.
- System libraries as packages: `ndk::log`, `ndk::android` and `ndk::atomic` give
  `-llog`, `-landroid`, `-latomic` to the link of whatever depends on them
  (`target_link_libraries(t log android atomic)` upstream).

Use: `uses = ["buildlog-cpp", "buildlog-ndk"]` in the project.
Size: `layers/L2_ndk.dl` (Datalog) and `ndk.py` (probe); see `case-studies/halide/apps/README.md`.
