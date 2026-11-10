#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The flatbuffers case study's ground truth: flatbuffers v25.12.19's own CMake
# build (Ninja), and CMake's cross attempts for x86_64 and universal2 on an
# arm64 Mac.
#
#   run-cmake.sh [VARIANT...]      (default: all of them, in this order)
#
# Variants:
#   release        Release, default options, native arm64; build, ctest, install
#   x86_64-naive   + -DCMAKE_OSX_ARCHITECTURES=x86_64: CMake builds flatc for
#                  x86_64 and runs it during the build (works here only through
#                  Rosetta 2)
#   universal2-naive + -DCMAKE_OSX_ARCHITECTURES="arm64;x86_64": a fat flatc
#   x86_64-twotree the documented workaround: a host tree that builds flatc,
#                  then an x86_64 tree with -DFLATBUFFERS_FLATC_EXECUTABLE=<host flatc>
#   ios-naive      a cross build no emulator can rescue (CMAKE_SYSTEM_NAME=iOS):
#                  configure and build until the first flatc run
#
# Environment overrides:
#   FLATBUFFERS_SRC  the checkout (default ../upstream, the submodule,
#                    at v25.12.19 = 7e163021). It is only read: every build is out of tree.
#   RAW              where build trees and logs go (default <repo>/build/case-flatbuffers/raw)
#   JOBS             build parallelism (default 4; the machine is shared)
#
# On Linux (linux/run.sh baselines) only `release` exists, into baseline/linux/release
# (the image's GCC) or baseline/linux-clang/release (BASELINE_FAMILY=clang, Clang 18);
# ELF's readelf and nm -D replace otool and nm -gU, sha256sum shasum.
#
# The committed files next to this script are normalized: the source, build and
# install directories are replaced by $SRC, $BUILD, $HOSTBUILD and $PREFIX.
# commands.txt is `ninja -t commands` (every edge, in dependency order).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
SRC="$(cd "${FLATBUFFERS_SRC:-$HERE/../upstream}" && pwd)"
RAW="${RAW:-$REPO/build/case-flatbuffers/raw}"
JOBS="${JOBS:-4}"
PIN=7e163021e59cca4f8e1e35a7c828b5c6b7915953
OS="$(uname -s)"
OUTROOT="$HERE"
if [[ "$OS" == Linux ]]; then
    OUTROOT="$HERE/linux"
    if [[ "${BASELINE_FAMILY:-gcc}" == clang ]]; then
        OUTROOT="$HERE/linux-clang"
        export CC=clang CXX=clang++
    fi
    mkdir -p "$OUTROOT"
fi

if [[ "$(git -C "$SRC" rev-parse HEAD)" != "$PIN" ]]; then
    echo "run-cmake.sh: $SRC is not at v25.12.19 ($PIN)" >&2
    exit 1
fi
if [[ -n "$(git -C "$SRC" status --porcelain)" ]]; then
    echo "run-cmake.sh: $SRC has local changes or untracked files" >&2
    exit 1
fi
mkdir -p "$RAW"

norm() {
    sed -e "s#$RAW/[a-z0-9_-]*-host/build#\$HOSTBUILD#g" \
        -e "s#$RAW/[a-z0-9_-]*/build#\$BUILD#g" \
        -e "s#$RAW/[a-z0-9_-]*/install#\$PREFIX#g" \
        -e "s#$SRC#\$SRC#g"
}

environment() {
    echo "# Environment"
    uname -sm
    if [[ "$OS" == Darwin ]]; then sw_vers -productVersion; else . /etc/os-release && echo "$PRETTY_NAME"; fi
    cmake --version | head -1
    echo "ninja $(ninja --version)"
    ${CXX:-c++} --version | head -1
    echo "flatbuffers $(git -C "$SRC" describe --tags) $(git -C "$SRC" rev-parse HEAD)"
    if [[ "$OS" == Darwin ]]; then
        if arch -x86_64 /usr/bin/true 2>/dev/null; then echo "Rosetta 2: installed"; else echo "Rosetta 2: absent"; fi
    fi
}

configure() { # dir, extra args...
    local dir="$1"; shift
    rm -rf "$dir"
    cmake -S "$SRC" -B "$dir/build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$dir/install" "$@"
}

# The command graph, the header dependencies ninja recorded, and what flatc is.
dump() { # variant, dir
    local v="$1" dir="$2" out="$OUTROOT/$1"
    mkdir -p "$out"
    ninja -C "$dir/build" -t commands | norm > "$out/commands.txt"
    ninja -C "$dir/build" -t deps | norm > "$dir/deps.txt" || true
    if [[ -x "$dir/build/flatc" ]]; then
        { echo "===== file flatc"; file "$dir/build/flatc" | norm; } > "$out/flatc-arch.txt"
    fi
}

SHA256=(shasum -a 256)
[[ "$OS" == Linux ]] && SHA256=(sha256sum)

variant_release() {
    local dir="$RAW/release" out="$OUTROOT/release"
    mkdir -p "$out"
    configure "$dir" > "$dir.log" 2>&1
    {
        cmake --build "$dir/build" -j "$JOBS" --verbose
        ctest --test-dir "$dir/build" --output-on-failure
        cmake --install "$dir/build"
    } >> "$dir.log" 2>&1
    dump release "$dir"
    local b="$dir/build"
    grep -E '^(FLATBUFFERS_HAS_[A-Z_]+|HAVE_REALPATH)(:[A-Z]+)?=' "$b/CMakeCache.txt" | sort > "$out/checks.txt"
    # The generated headers, byte for byte, and which object read which one.
    (cd "$b" && find . -name '*_generated.h' -o -name '*.fbs.h' | sort) > "$out/generated-files.txt"
    (cd "$b" && find . \( -name '*_generated.h' -o -name '*.fbs.h' \) -exec "${SHA256[@]}" {} + | sort -k2) \
        > "$out/generated-sha256.txt"
    grep -E '^[^ ].*: #deps|_generated\.h$|\.fbs\.h$' "$dir/deps.txt" |
        awk '/#deps/ {o=$1} !/#deps/ {print o, $1}' | sort -u > "$out/generated-header-deps.txt"
    { echo "===== libflatbuffers.a members"; ar t "$b/libflatbuffers.a" | sort
      if [[ "$OS" == Linux ]]; then
        echo "===== nm -g --defined-only libflatbuffers.a (defined globals, sorted)"
        nm -g --defined-only "$b/libflatbuffers.a" 2>/dev/null | grep -v ':$' | awk 'NF {print $NF}' | sort -u
      else
        echo "===== nm -gU libflatbuffers.a (defined globals, sorted)"
        nm -gU "$b/libflatbuffers.a" | grep -v ':$' | awk 'NF {print $NF}' | sort -u
      fi
    } > "$out/exported-symbols.txt"
    if [[ "$OS" == Linux ]]; then
      { for exe in flatc flattests flatsamplebinary flatsampletext flatsamplebfbs; do
          echo "===== readelf -d $exe"
          readelf -d "$b/$exe" | grep -E 'NEEDED|RUNPATH|RPATH' | awk '{$1=""; print substr($0,2)}'
        done; } | norm > "$out/binaries.txt"
    else
    { for exe in flatc flattests flatsamplebinary flatsampletext flatsamplebfbs; do
        echo "===== otool -L $exe"; otool -L "$b/$exe" | tail -n +2
      done; } | norm > "$out/binaries.txt"
    fi
    { echo "===== flatc --version"; "$b/flatc" --version
      for exe in flatsamplebinary flatsampletext flatsamplebfbs; do
        echo "===== $exe"; (cd "$SRC" && "$b/$exe")
      done; } > "$out/program-output.txt" 2>&1
    grep -E '^ *[0-9]+/[0-9]+ Test|tests passed' "$dir.log" | sed -E 's/ +[0-9.]+ sec$//' > "$out/ctest.txt"
    (cd "$dir/install" && find . -type f -o -type l | sort) > "$out/install-manifest.txt"
    for f in flatbuffers-config-version.cmake FlatBuffersTargets.cmake FlatBuffersTargets-release.cmake \
             FlatcTargets.cmake FlatcTargets-release.cmake; do
        norm < "$dir/install/lib/cmake/flatbuffers/$f" > "$out/$f"
    done
    norm < "$dir/install/lib/pkgconfig/flatbuffers.pc" > "$out/flatbuffers.pc"
}

# A single-tree cross attempt: the build either succeeds (with a target-arch
# flatc that some emulator ran) or stops at the first flatc run.
variant_naive() { # variant, archs
    local v="$1" archs="$2" dir="$RAW/$1" out="$HERE/$1"
    configure "$dir" "-DCMAKE_OSX_ARCHITECTURES=$archs" > "$dir.log" 2>&1
    local rc=0
    cmake --build "$dir/build" -j "$JOBS" --verbose >> "$dir.log" 2>&1 || rc=$?
    ctest --test-dir "$dir/build" --output-on-failure >> "$dir.log" 2>&1 || true
    dump "$v" "$dir"
    local b="$dir/build"
    { echo "build exit status: $rc"
      for f in flatc flattests flatsamplebinary libflatbuffers.a; do
        [[ -e "$b/$f" ]] && { printf '%s: ' "$f"; lipo -archs "$b/$f"; }
      done
      echo "===== flatc runs during the build (custom commands)"
      grep -c '/flatc --cpp' "$dir.log" || true
      echo "===== ctest"
      grep -E '^ *[0-9]+/[0-9]+ Test|tests passed|tests failed' "$dir.log" | sed -E 's/ +[0-9.]+ sec$//'
    } > "$out/outcome.txt"
}

variant_x86_64_twotree() {
    local host="$RAW/x86_64-twotree-host" dir="$RAW/x86_64-twotree" out="$HERE/x86_64-twotree"
    # Tree 1: flatc for the machine running the build. Tests off, or CMake builds
    # (and generates for) them too; nothing else is needed from this tree.
    configure "$host" -DFLATBUFFERS_BUILD_TESTS=OFF -DFLATBUFFERS_BUILD_FLATLIB=OFF \
        -DFLATBUFFERS_INSTALL=OFF > "$host.log" 2>&1
    cmake --build "$host/build" -j "$JOBS" --target flatc --verbose >> "$host.log" 2>&1
    # Tree 2: the target, pointed at tree 1's flatc by path.
    configure "$dir" -DCMAKE_OSX_ARCHITECTURES=x86_64 \
        "-DFLATBUFFERS_FLATC_EXECUTABLE=$host/build/flatc" > "$dir.log" 2>&1
    local rc=0
    cmake --build "$dir/build" -j "$JOBS" --verbose >> "$dir.log" 2>&1 || rc=$?
    ctest --test-dir "$dir/build" --output-on-failure >> "$dir.log" 2>&1 || true
    dump x86_64-twotree "$dir"
    ninja -C "$host/build" -t commands flatc | norm > "$out/host-commands.txt"
    local b="$dir/build"
    { echo "build exit status: $rc"
      printf 'host flatc: '; lipo -archs "$host/build/flatc"
      for f in flatc flattests flatsamplebinary libflatbuffers.a; do
        [[ -e "$b/$f" ]] && { printf '%s: ' "$f"; lipo -archs "$b/$f"; }
      done
      echo "===== ctest"
      grep -E '^ *[0-9]+/[0-9]+ Test|tests passed|tests failed' "$dir.log" | sed -E 's/ +[0-9.]+ sec$//'
    } > "$out/outcome.txt"
}

variant_ios_naive() {
    local dir="$RAW/ios-naive" out="$HERE/ios-naive"
    mkdir -p "$out"
    # FLATBUFFERS_INSTALL=OFF and CMAKE_MACOSX_BUNDLE=OFF: otherwise configure stops at
    # install(TARGETS flatc) (an iOS executable is a bundle by default).
    configure "$dir" -DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_ARCHITECTURES=arm64 \
        -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 -DFLATBUFFERS_INSTALL=OFF \
        -DCMAKE_MACOSX_BUNDLE=OFF > "$dir.log" 2>&1 || true
    local rc=0
    cmake --build "$dir/build" -j "$JOBS" --verbose -- -k 0 >> "$dir.log" 2>&1 || rc=$?
    { echo "build exit status: $rc"
      [[ -e "$dir/build/flatc" ]] && { echo "===== file flatc"; file "$dir/build/flatc" | norm
          echo "===== vtool -show-build flatc"; vtool -show-build "$dir/build/flatc" | grep -E 'platform|minos'; }
      echo "===== failures"
      grep -E '^FAILED:|Bad CPU|cannot execute|Exec format|Killed|error:' "$dir.log" | norm | sort | uniq -c | head -40
    } > "$out/outcome.txt"
}

run_variant() {
    case "$1" in
        release) variant_release ;;
        x86_64-naive) variant_naive x86_64-naive x86_64 ;;
        universal2-naive) variant_naive universal2-naive "arm64;x86_64" ;;
        x86_64-twotree) variant_x86_64_twotree ;;
        ios-naive) variant_ios_naive ;;
        *) echo "run-cmake.sh: unknown variant $1" >&2; exit 2 ;;
    esac
}

environment > "$OUTROOT/environment.txt"
if [[ $# -eq 0 ]]; then
    set -- release x86_64-naive universal2-naive x86_64-twotree ios-naive
    [[ "$OS" == Linux ]] && set -- release
fi
if [[ "$OS" == Linux && "$*" != release ]]; then echo "run-cmake.sh: only release on Linux" >&2; exit 2; fi
for v in "$@"; do
    echo "== $v"
    run_variant "$v"
done
echo "baseline written to $OUTROOT"
