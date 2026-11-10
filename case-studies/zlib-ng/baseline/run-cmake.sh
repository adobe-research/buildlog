#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The zlib-ng case study's ground truth: zlib-ng 2.3.3's own CMake build
# (Release, Ninja, every option at its default) for three slices of this Mac:
#
#   run-cmake.sh native      arm64, the host (CMAKE_OSX_ARCHITECTURES unset)
#   run-cmake.sh x86_64-default      cross to x86_64 (CMAKE_OSX_ARCHITECTURES=x86_64),
#                                    defaults: fails linking gtest_zlib (Homebrew's
#                                    GTest is arm64-only); the failure is recorded
#   run-cmake.sh x86_64              the same with -DWITH_GTEST=OFF
#   run-cmake.sh universal2-default  CMAKE_OSX_ARCHITECTURES="arm64;x86_64" (zlib-ng
#                                    #1016/#1085), defaults: fails the same way
#   run-cmake.sh universal2          the same with -DWITH_GTEST=OFF
#   run-cmake.sh all                 all five, in that order
#
# Environment overrides:
#   ZLIBNG_SRC  the checkout (default ../upstream, the submodule, at 2.3.3 = 12731092).
#               It is only read: the build is out of tree.
#   RAW         where build trees and logs go (default <repo>/build/case-zlib-ng/raw).
#
# The committed files next to this script are normalized: the source and
# build directories are replaced by $SRC and $BUILD.
#
# On Linux (linux/run.sh baselines) only `native` exists: the host's slice,
# compiled by the GCC the image has (baseline/linux/native) or, with
# BASELINE_FAMILY=clang, by its Clang 18 (baseline/linux-clang/native). ELF's
# tools stand in for Mach-O's (readelf -d for otool -L, nm -D for nm -gU), and
# GTest comes from the image's /opt/googletest.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
SRC="$(cd "${ZLIBNG_SRC:-$HERE/../upstream}" && pwd)"
RAW="${RAW:-$REPO/build/case-zlib-ng/raw}"
PIN=12731092979c6d07f42da27da673a9f6c7b13586
OS="$(uname -s)"
OUTROOT="$HERE"
EXTRA=()
if [[ "$OS" == Linux ]]; then
    OUTROOT="$HERE/linux"
    if [[ "${BASELINE_FAMILY:-gcc}" == clang ]]; then
        OUTROOT="$HERE/linux-clang"
        export CC=clang CXX=clang++
    fi
    mkdir -p "$OUTROOT"
    EXTRA=(-DCMAKE_PREFIX_PATH=/opt/googletest)
fi

if [[ "$(git -C "$SRC" rev-parse HEAD)" != "$PIN" ]]; then
    echo "run-cmake.sh: $SRC is not at 2.3.3 ($PIN)" >&2
    exit 1
fi
if [[ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]]; then
    echo "run-cmake.sh: $SRC has local changes" >&2
    exit 1
fi
mkdir -p "$RAW"

{
    echo "# Environment"
    uname -sm
    if [[ "$OS" == Darwin ]]; then sw_vers -productVersion; else . /etc/os-release && echo "$PRETTY_NAME"; fi
    cmake --version | head -1
    echo "ninja $(ninja --version)"
    cc --version | head -1
    echo "zlib-ng $(git -C "$SRC" describe --tags) $(git -C "$SRC" rev-parse HEAD)"
} > "$OUTROOT/environment.txt"

variant() {
    local name="$1"; shift
    local BUILD="$RAW/cmake-$name"
    local OUT="$OUTROOT/$name"
    rm -rf "$BUILD" "$OUT"
    mkdir -p "$BUILD" "$OUT"
    norm() { sed -e "s#$BUILD#\$BUILD#g" -e "s#$SRC#\$SRC#g"; }
    local status=0
    {
        cmake -S "$SRC" -B "$BUILD" -G Ninja -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ${EXTRA[@]+"${EXTRA[@]}"} "$@" &&
        cmake --build "$BUILD" -j4 --verbose &&
        ctest --test-dir "$BUILD" -j4 --output-on-failure
    } > "$RAW/cmake-$name.log" 2>&1 || status=$?
    norm < "$RAW/cmake-$name.log" > "$RAW/cmake-$name.norm.log"
    echo "exit status: $status" > "$OUT/status.txt"
    grep -E '^-- (Arch|Basearch|Architecture-specific|Performing Test|Looking for|Check)' \
        "$RAW/cmake-$name.norm.log" | sed -E 's/ - (Success|Failed|found|not found)$/ -> \1/' \
        > "$OUT/configure-checks.txt" || true
    if [[ -f "$BUILD/build.ninja" ]]; then
        ninja -C "$BUILD" -t commands | norm > "$OUT/commands.txt"
    fi
    if [[ -f "$BUILD/CMakeCache.txt" ]]; then
        grep -E '^(HAVE_[A-Z0-9_]+|NEON_[A-Z0-9_]+|ARM_[A-Z0-9_]+|[A-Z0-9_]*FLAG|WITH_[A-Z0-9_]+|FNO_LTO_AVAILABLE|[A-Z_]*OFF64_T)(:[A-Z]+)?=' \
            "$BUILD/CMakeCache.txt" | sort > "$OUT/cache.txt"
    fi
    if [[ $status -ne 0 ]]; then
        # The failure: the first compiler errors, normalized.
        grep -E -B2 -A3 'error:|FAILED:' "$RAW/cmake-$name.norm.log" | head -60 > "$OUT/failure.txt" || true
        echo "$name: failed (status $status), see $OUT/failure.txt"
        return 0
    fi
    for f in zconf-ng.h zlib-ng.h zlib_name_mangling-ng.h zlib-ng.pc gzread_mangle.h; do
        norm < "$BUILD/$f" > "$OUT/$f"
    done
    if [[ "$OS" == Linux ]]; then
        {
            echo "===== libz-ng.so.2.3.3"
            nm -D --defined-only "$BUILD/libz-ng.so.2.3.3" | awk '{print $2, $3}' | sort -k2
            echo "===== libz-ng.a"
            nm -g --defined-only "$BUILD/libz-ng.a" | grep -v '^$' | grep -v ':$' | sort -k3
        } | norm > "$OUT/exported-symbols.txt"
        {
            echo "===== links"
            for f in libz-ng.so.2 libz-ng.so; do echo "$f -> $(readlink "$BUILD/$f")"; done
            echo "===== readelf -d libz-ng.so.2.3.3"
            readelf -d "$BUILD/libz-ng.so.2.3.3" | grep -E 'NEEDED|SONAME|RUNPATH|RPATH' | awk '{$1=""; print substr($0,2)}'
            for exe in example minigzip minideflate infcover; do
                echo "===== readelf -d $exe"
                readelf -d "$BUILD/$exe" | grep -E 'NEEDED|RUNPATH|RPATH' | awk '{$1=""; print substr($0,2)}'
            done
            echo "===== ar -t libz-ng.a"
            ar -t "$BUILD/libz-ng.a" | sort
        } | norm > "$OUT/elf-metadata.txt"
        {
            echo "===== $(uname -m)"
            nm "$BUILD/libz-ng.a" 2>/dev/null | awk '$2 == "T" {print $3}' |
                grep -E '_(neon|armv8|armv6|sse2|ssse3|sse41|sse42|pclmulqdq|avx2|avx512|avx512_vnni|vpclmulqdq)$|^(cpu_check_features|arm_check_features|x86_check_features)$' | sort -u
        } > "$OUT/isa-functions.txt"
        grep -E '^ *[0-9]+/[0-9]+ Test|tests passed|tests failed' "$RAW/cmake-$name.log" |
            sed -E 's/ +[0-9.]+ sec$//' > "$OUT/ctest.txt"
        echo "$name: ok"
        return 0
    fi
    {
        for f in libz-ng.2.3.3.dylib libz-ng.a; do
            echo "===== $f"
            nm -gU "$BUILD/$f" | sort -k3 | norm
        done
    } > "$OUT/exported-symbols.txt"
    {
        echo "===== lipo -archs"
        for f in libz-ng.2.3.3.dylib libz-ng.a example minigzip; do
            echo "$f: $(lipo -archs "$BUILD/$f")"
        done
        echo "===== links"
        for f in libz-ng.2.dylib libz-ng.dylib; do echo "$f -> $(readlink "$BUILD/$f")"; done
        echo "===== otool -L libz-ng.2.3.3.dylib"
        otool -L "$BUILD/libz-ng.2.3.3.dylib" | tail -n +2
        for exe in example minigzip minideflate infcover; do
            echo "===== otool -L $exe"
            otool -L "$BUILD/$exe" | tail -n +2
        done
        echo "===== ar -t libz-ng.a"
        for a in $(lipo -archs "$BUILD/libz-ng.a"); do
            echo "--- $a"; lipo -thin "$a" -output "$BUILD/libz-ng.$a.a" "$BUILD/libz-ng.a" 2>/dev/null \
                && ar -t "$BUILD/libz-ng.$a.a" | sort || ar -t "$BUILD/libz-ng.a" | sort
        done
    } | norm > "$OUT/dylib-metadata.txt"
    # The ISA-specific functions each slice of the static library defines:
    # what the runtime dispatch (functable.c) can choose from.
    {
        for a in $(lipo -archs "$BUILD/libz-ng.a"); do
            echo "===== $a"
            nm -arch "$a" "$BUILD/libz-ng.a" 2>/dev/null | awk '$2 == "T" {print $3}' |
                grep -E '_(neon|armv8|armv6|sse2|ssse3|sse41|sse42|pclmulqdq|avx2|avx512|avx512_vnni|vpclmulqdq)$|^_(cpu_check_features|arm_check_features|x86_check_features)$' | sort -u
        done
    } > "$OUT/isa-functions.txt"
    grep -E '^ *[0-9]+/[0-9]+ Test|tests passed|tests failed' "$RAW/cmake-$name.log" |
        sed -E 's/ +[0-9.]+ sec$//' > "$OUT/ctest.txt"
    echo "$name: ok"
}

X86=-DCMAKE_OSX_ARCHITECTURES=x86_64
U2="-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64"
if [[ "$OS" == Linux && "${1:-all}" != native && "${1:-all}" != all ]]; then
    echo "run-cmake.sh: only native on Linux" >&2
    exit 2
fi
case "${1:-all}" in
    native) variant native ;;
    x86_64-default) variant x86_64-default "$X86" ;;
    x86_64) variant x86_64 "$X86" -DWITH_GTEST=OFF ;;
    universal2-default) variant universal2-default "$U2" ;;
    universal2) variant universal2 "$U2" -DWITH_GTEST=OFF ;;
    all)
        [[ "$OS" == Linux ]] && { variant native; exit 0; }
        variant native
        variant x86_64-default "$X86"
        variant x86_64 "$X86" -DWITH_GTEST=OFF
        variant universal2-default "$U2"
        variant universal2 "$U2" -DWITH_GTEST=OFF
        ;;
    *) echo "usage: run-cmake.sh native|x86_64-default|x86_64|universal2-default|universal2|all" >&2; exit 2 ;;
esac
