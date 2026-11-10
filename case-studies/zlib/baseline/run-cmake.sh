#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The zlib case study's ground truth: zlib v1.3.2's own CMake build (Release,
# Ninja), its tests, and the artifacts the buildlog phases are compared with.
# Adapted from the preparation run's script.
#
#   run-cmake.sh
#
# Environment overrides:
#   ZLIB_SRC   the zlib checkout (default ../upstream, the submodule, at v1.3.2 = da607da7).
#              It is only read: the build is out of tree.
#
#   BASELINE_RAW  where raw trees and logs go (default baseline/raw, or
#              baseline/linux/raw on Linux)
#
# Raw trees and logs go to baseline/raw/ (gitignored). The committed files next to
# this script are normalized: the source, build and install directories are
# replaced by $SRC, $BUILD and $PREFIX. On Linux (linux/run.sh baselines) the
# committed files go to baseline/linux/, with ELF's tools in place of Mach-O's:
# readelf -d for otool -L, nm -D for nm -gU, libz.so.* for libz.*.dylib.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "${ZLIB_SRC:-$HERE/../upstream}" && pwd)"
OS="$(uname -s)"
OUT="$HERE"
[[ "$OS" == Linux ]] && OUT="$HERE/linux"
mkdir -p "$OUT"
RAW="${BASELINE_RAW:-$OUT/raw}"
BUILD="$RAW/cmake-build"
PREFIX="$RAW/cmake-install"
PIN=da607da739fa6047df13e66a2af6b8bec7c2a498
git() { command git -c safe.directory="$SRC" "$@"; }

if [[ "$(git -C "$SRC" rev-parse HEAD)" != "$PIN" ]]; then
    echo "run-cmake.sh: $SRC is not at v1.3.2 ($PIN)" >&2
    exit 1
fi
if [[ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]]; then
    echo "run-cmake.sh: $SRC has local changes" >&2
    exit 1
fi

rm -rf "$BUILD" "$PREFIX"
mkdir -p "$BUILD" "$PREFIX"

norm() { sed -e "s#$BUILD#\$BUILD#g" -e "s#$PREFIX#\$PREFIX#g" -e "s#$SRC#\$SRC#g"; }

{
    echo "# Environment"
    uname -sm
    if [[ "$OS" == Darwin ]]; then sw_vers -productVersion; else . /etc/os-release && echo "$PRETTY_NAME"; fi
    cmake --version | head -1
    echo "ninja $(ninja --version)"
    cc --version | head -1
    echo "zlib $(git -C "$SRC" describe --tags) $(git -C "$SRC" rev-parse HEAD)"
} > "$RAW/environment.txt"

{
    cmake -S "$SRC" -B "$BUILD" -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
    cmake --build "$BUILD" --verbose
    ctest --test-dir "$BUILD" --output-on-failure
    cmake --install "$BUILD"
} > "$RAW/cmake-run.log" 2>&1

HERE="$OUT"
cp "$RAW/environment.txt" "$HERE/environment.txt"
ninja -C "$BUILD" -t commands | norm > "$HERE/commands.txt"
# The configure-time checks: the cache entries of the check variables.
grep -E '^(HAVE_[A-Z0-9_]+|OFF64_T)(:[A-Z]+)?=' "$BUILD/CMakeCache.txt" | sort > "$HERE/checks.txt"
norm < "$BUILD/zconf.h" > "$HERE/zconf.h"
norm < "$BUILD/zlib.pc" > "$HERE/zlib.pc"
if [[ "$OS" == Linux ]]; then
    {
        echo "===== libz.so.1.3.2"
        nm -D --defined-only "$BUILD/libz.so.1.3.2" | grep -v ' [a-z] '
        echo "===== libz.a"
        nm -g --defined-only "$BUILD/libz.a"
    } > "$HERE/exported-symbols.txt"
    {
        echo "===== links"
        for f in libz.so.1 libz.so; do echo "$f -> $(readlink "$BUILD/$f")"; done
        echo "===== readelf -d libz.so.1.3.2"
        readelf -d "$BUILD/libz.so.1.3.2" | grep -E 'NEEDED|SONAME|RUNPATH|RPATH' | awk '{$1=""; print substr($0,2)}'
        for exe in zlib_example minigzip zlib_static_example static_minigzip; do
            echo "===== readelf -d test/$exe"
            readelf -d "$BUILD/test/$exe" | grep -E 'NEEDED|RUNPATH|RPATH' | awk '{$1=""; print substr($0,2)}'
        done
    } | norm > "$HERE/elf-metadata.txt"
    (cd "$PREFIX" && find . \( -type f -o -type l \) | sed 's#^\./##' | sort) > "$HERE/install-manifest.txt"
else
{
    for f in libz.1.3.2.dylib libz.a; do
        echo "===== $f"
        nm -gU "$BUILD/$f"
    done
} > "$HERE/exported-symbols.txt"
{
    echo "===== links"
    for f in libz.1.dylib libz.dylib; do echo "$f -> $(readlink "$BUILD/$f")"; done
    echo "===== otool -L libz.1.3.2.dylib"
    otool -L "$BUILD/libz.1.3.2.dylib" | tail -n +2
    echo "===== LC_ID_DYLIB"
    otool -l "$BUILD/libz.1.3.2.dylib" | grep -A5 LC_ID_DYLIB | grep -E 'name|version'
    for exe in zlib_example minigzip zlib_static_example static_minigzip; do
        echo "===== otool -L test/$exe"
        otool -L "$BUILD/test/$exe" | tail -n +2
    done
} | norm > "$HERE/dylib-metadata.txt"
fi
# What the test programs ctest runs print (each in a fresh directory: they
# write foo.gz into the current one).
{
    for exe in zlib_example zlib_static_example; do
        echo "===== $exe"
        rm -rf "$RAW/run" && mkdir -p "$RAW/run"
        (cd "$RAW/run" && "$BUILD/test/$exe")
    done
    rm -rf "$RAW/run"
} > "$HERE/example-output.txt"
grep -E '^ *[0-9]+/[0-9]+ Test|tests passed' "$RAW/cmake-run.log" |
    sed -E 's/ +[0-9.]+ sec$//' > "$HERE/ctest.txt"
echo "baseline written to $HERE"
