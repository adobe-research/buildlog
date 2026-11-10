# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Toolchain file: macOS arm64 host -> aarch64-linux-gnu target (M3 phase 7c).
#
# Homebrew's llvm@22 clang with --target=aarch64-linux-gnu, Homebrew's lld@22, and the
# sysroot exported from the Linux image by ../sysroot.sh. Pass the sysroot on the command
# line, -DCMAKE_SYSROOT=..., so that try_compile projects inherit it.
#
# Modeled on Halide's cmake/toolchain.linux-aarch64.cmake (which uses a GCC cross
# compiler and qemu), with the compiler swapped for clang and no emulator.

set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

set(_llvm "/opt/homebrew/opt/llvm@22")
set(_lld "/opt/homebrew/opt/lld@22")

set(CMAKE_C_COMPILER "${_llvm}/bin/clang")
set(CMAKE_CXX_COMPILER "${_llvm}/bin/clang++")
set(CMAKE_C_COMPILER_TARGET aarch64-linux-gnu)
set(CMAKE_CXX_COMPILER_TARGET aarch64-linux-gnu)
set(CMAKE_AR "${_llvm}/bin/llvm-ar" CACHE FILEPATH "")
set(CMAKE_RANLIB "${_llvm}/bin/llvm-ranlib" CACHE FILEPATH "")

# llvm@22 does not ship lld; lld@22 is a separate keg, so name the linker by path.
set(CMAKE_LINKER "${_lld}/bin/ld.lld" CACHE FILEPATH "")
foreach (kind IN ITEMS EXE SHARED MODULE)
    set(CMAKE_${kind}_LINKER_FLAGS_INIT "--ld-path=${_lld}/bin/ld.lld")
endforeach ()

set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
