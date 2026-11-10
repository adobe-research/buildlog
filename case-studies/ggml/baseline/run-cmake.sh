#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The ggml case study's ground truth: ggml v0.25.3's own CMake build on this Mac,
# in the configurations the buildlog project is compared with.
#
#   run-cmake.sh [CONFIG...]     (default: all of them)
#
# CONFIG is one of
#   default          CMake's defaults (Release; on Apple: Metal, BLAS, GGML_NATIVE on);
#                    everything is built and ctest runs.
#   cpu              the scope of the buildlog study's first part: the core and
#                    the CPU backend, native arm64 (-DGGML_METAL=OFF -DGGML_BLAS=OFF).
#                    Everything is built and ctest runs.
#   variants-arm64   -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DGGML_NATIVE=OFF
#                    (Metal and BLAS off): the loader and the apple_m* modules.
#   variants-x86_64  the same, -DCMAKE_OSX_ARCHITECTURES=x86_64: the x86 variant set;
#                    the smoke test runs under Rosetta 2.
#   variants-native  the variants options with GGML_NATIVE left at its default (ON):
#                    configure only, to record what CMake does with the combination.
#
# On Linux (linux/run.sh baselines) `cpu` and variants-arm64 are built (Metal
# is not an option there; Linux's ARM variant list, armv8.0_1 … armv9.2_2; with
# GCC 13 the two armv9.2 modules fail to compile, build-status.txt), into
# baseline/linux/cpu, or baseline/linux-clang/cpu with BASELINE_FAMILY=clang
# (Clang 18 instead of GCC); ELF's readelf and nm -D replace otool and nm -gU.
#
# Environment overrides:
#   GGML_SRC   the ggml checkout (default ../upstream, the submodule, at v0.25.3 = 353b63b4).
#              It is only read: the builds are out of tree.
#
# Raw trees and logs go to $GGML_BASELINE_RAW (default <repo>/build/case-ggml/baseline-raw),
# untracked. The committed files under
# baseline/<CONFIG>/ are normalized: $SRC and $BUILD replace the source and build dirs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
SRC="$(cd "${GGML_SRC:-$HERE/../upstream}" && pwd)"
RAW="${GGML_BASELINE_RAW:-$REPO/build/case-ggml/baseline-raw}"
PIN=353b63b439f27ab2cc19dac97ab1681ba6d2d084
JOBS="${JOBS:-4}"
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
    echo "run-cmake.sh: $SRC is not at v0.25.3 ($PIN)" >&2
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
    if [[ "$OS" == Darwin ]]; then
        sw_vers -productVersion
        sysctl -n machdep.cpu.brand_string
    else
        . /etc/os-release && echo "$PRETTY_NAME"
    fi
    cmake --version | head -1
    echo "ninja $(ninja --version)"
    cc --version | head -1
    echo "ggml $(git -C "$SRC" describe --tags) $(git -C "$SRC" rev-parse HEAD)"
} > "$OUTROOT/environment.txt"

# Only the targets in the study's scope are built in the variants configurations.
# examples/CMakeLists.txt skips examples/simple under GGML_BACKEND_DL, so the
# smoke test there is tests/test-backend-ops, which CMake builds in every mode.
VARIANT_TARGETS=(ggml test-backend-ops)

run() {
    local cfg="$1"; shift
    local BUILD="$RAW/$cfg" OUT="$OUTROOT/$cfg"
    norm() { sed -e "s#$BUILD#\$BUILD#g" -e "s#$SRC#\$SRC#g"; }
    rm -rf "$BUILD" "$OUT"; mkdir -p "$BUILD" "$OUT"
    local opts=(-G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CCACHE=OFF)
    case "$cfg" in
        default) ;;
        cpu) opts+=(-DGGML_METAL=OFF -DGGML_BLAS=OFF) ;;
        variants-arm64) opts+=(-DGGML_METAL=OFF -DGGML_BLAS=OFF -DGGML_BACKEND_DL=ON
                               -DGGML_CPU_ALL_VARIANTS=ON -DGGML_NATIVE=OFF) ;;
        variants-x86_64) opts+=(-DGGML_METAL=OFF -DGGML_BLAS=OFF -DGGML_BACKEND_DL=ON
                                -DGGML_CPU_ALL_VARIANTS=ON -DGGML_NATIVE=OFF
                                -DCMAKE_OSX_ARCHITECTURES=x86_64) ;;
        variants-native) opts+=(-DGGML_METAL=OFF -DGGML_BLAS=OFF -DGGML_BACKEND_DL=ON
                                -DGGML_CPU_ALL_VARIANTS=ON) ;;
        *) echo "unknown config $cfg" >&2; exit 1 ;;
    esac
    echo "cmake ${opts[*]}" > "$OUT/cmake-options.txt"
    local ok=0
    cmake -S "$SRC" -B "$BUILD" "${opts[@]}" > "$BUILD/configure.log" 2>&1 || ok=$?
    norm < "$BUILD/configure.log" > "$OUT/configure.log"
    if [[ $ok != 0 || "$cfg" == variants-native ]]; then
        echo "configure exit status $ok" >> "$OUT/configure.log"
        [[ -f "$BUILD/build.ninja" ]] && ninja -C "$BUILD" -t commands ggml | norm > "$OUT/commands.txt"
        return 0
    fi
    grep -E '^(GGML_[A-Z0-9_]+|BUILD_SHARED_LIBS|CMAKE_BUILD_TYPE|CMAKE_OSX_[A-Z_]+|HAVE_[A-Z0-9_]+|GGML_COMPILER_[A-Z0-9_]+):' \
        "$BUILD/CMakeCache.txt" | sort > "$OUT/cache.txt"
    case "$cfg" in
        default|cpu)
            cmake --build "$BUILD" -j "$JOBS" > "$BUILD/build.log" 2>&1
            ninja -C "$BUILD" -t commands | norm > "$OUT/commands.txt"
            (cd "$BUILD" && ctest -j "$JOBS" --output-on-failure > "$BUILD/ctest.log" 2>&1) || true
            grep -E '^ *[0-9]+/[0-9]+ Test|tests passed|Not Run|Failed' "$BUILD/ctest.log" |
                sed -E 's/ +[0-9.]+ sec$//' | sort -k4 > "$OUT/ctest.txt"
            ;;
        variants-*)
            local targets=("${VARIANT_TARGETS[@]}")
            # every module library is a dependency of ggml (add_dependencies)
            # GCC 13 cannot build the armv9.2 levels (+sme): keep going and record the status.
            local st=0
            cmake --build "$BUILD" -j "$JOBS" --target "${targets[@]}" -- -k 0 > "$BUILD/build.log" 2>&1 || st=$?
            echo "build exit status $st" > "$OUT/build-status.txt"
            grep -E "^FAILED:" "$BUILD/build.log" | norm | sort >> "$OUT/build-status.txt" || true
            ninja -C "$BUILD" -t commands "${targets[@]}" | norm > "$OUT/commands.txt"
            ;;
    esac
    # compile_commands.json as JSONL (file, output, arguments), for comparisons
    python3 - "$BUILD/compile_commands.json" "$BUILD" "$SRC" > "$OUT/compile-commands.jsonl" <<'PY'
import json, shlex, sys
cc, build, src = sys.argv[1:]
def n(s): return s.replace(build, "$BUILD").replace(src, "$SRC")
for e in sorted(json.load(open(cc)), key=lambda e: (e.get("output", ""), e["file"])):
    args = e.get("arguments") or shlex.split(e["command"])
    print(json.dumps({"file": n(e["file"]), "output": n(e.get("output", "")), "args": [n(a) for a in args]}))
PY
    if [[ "$OS" == Linux ]]; then
        {
            echo "===== bin/"
            (cd "$BUILD/bin" && ls -1)
            echo "===== src/*.so*"
            (cd "$BUILD/src" && ls -1 *.so*)
            for f in "$BUILD"/src/*.so* "$BUILD"/src/ggml-cpu/*.so* "$BUILD"/bin/*.so; do
                [[ -e "$f" ]] || continue
                [[ -L "$f" ]] && { echo "===== $(basename "$f") -> $(readlink "$f")"; continue; }
                echo "===== $(basename "$f")"
                file -b "$f" | sed 's/, BuildID.*//'
                readelf -d "$f" | grep -E 'NEEDED|SONAME|RUNPATH|RPATH' | awk '{$1=""; print substr($0,2)}'
            done
        } | norm > "$OUT/artifacts.txt"
        {
            for f in "$BUILD"/bin/*.so "$BUILD"/src/libggml*.so* "$BUILD"/src/ggml-cpu/*.so*; do
                [[ -e "$f" && ! -L "$f" ]] || continue
                echo "===== $(basename "$f")"
                nm -D --defined-only "$f" | awk '{print $NF}' | sort
            done
        } > "$OUT/exported-symbols.txt"
    else
    {
        echo "===== bin/"
        (cd "$BUILD/bin" && ls -1)
        echo "===== src/*.dylib"
        (cd "$BUILD/src" && ls -1 *.dylib)
        for f in "$BUILD"/src/*.dylib "$BUILD"/src/ggml-cpu/*.dylib "$BUILD"/bin/*.so; do
            [[ -e "$f" ]] || continue
            [[ -L "$f" ]] && { echo "===== $(basename "$f") -> $(readlink "$f")"; continue; }
            echo "===== $(basename "$f")"
            file -b "$f"
            otool -L "$f" | tail -n +2
            otool -l "$f" | grep -A3 -E 'LC_ID_DYLIB|LC_RPATH' | grep -E 'name|path' || true
        done
    } | norm > "$OUT/artifacts.txt"
    {
        for f in "$BUILD"/bin/*.so "$BUILD"/src/libggml*.dylib "$BUILD"/src/ggml-cpu/*.dylib; do
            [[ -e "$f" && ! -L "$f" ]] || continue
            echo "===== $(basename "$f")"
            nm -gU "$f" | awk '{print $NF}' | sort
        done
    } > "$OUT/exported-symbols.txt"
    fi
    # The one configured file the libraries compile against.
    cp "$BUILD/src/ggml-version.h" "$OUT/ggml-version.h"
    # The smoke test: the program loads every backend it finds
    # (ggml_backend_load_all); the loader says which file it loaded. It runs
    # from another directory, so only the executable's directory is searched.
    local runner=() smoke=(bin/simple-backend)
    [[ "$cfg" == variants-x86_64 ]] && runner=(arch -x86_64)
    [[ "$cfg" == variants-* ]] && smoke=(bin/test-backend-ops -o ADD)
    (cd "$BUILD" && ${runner[@]+"${runner[@]}"} "${smoke[@]}") 2>&1 | norm > "$OUT/smoke.txt" || true
}

configs=("$@")

[[ $# -gt 0 ]] || configs=(default cpu variants-arm64 variants-x86_64 variants-native)
[[ $# -gt 0 || "$OS" != Linux ]] || configs=(cpu variants-arm64)
for c in "${configs[@]}"; do echo "== $c"; run "$c"; done
echo "baseline written to $OUTROOT"
