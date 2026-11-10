#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# M3 phase 7b: configure/build/test Halide with its own CMake build entirely inside the
# Linux/GCC image (linux/Dockerfile) and capture the ground truth, like
# ../baseline/run-cmake.sh does on macOS.
#
#   run-cmake-linux.sh [STEP...]
#
# STEP: configure build dump test normalize metrics
#       (default: configure build dump test normalize)
#
# Environment overrides:
#   HALIDE_SRC  Halide checkout whose git objects hold the pin (default ../upstream, the submodule).
#               `git archive 4e272cbfcc` of it is unpacked into the docker volume
#               buildlog-halide-linux-src, mounted at /src (see "source" below).
#   IMAGE       (default buildlog-halide-linux:22)
#   JOBS        ninja/ctest -j (default: the container's CPU count)
#   NINJA_TARGETS  targets for the build step (default: all)
#
# Container layout (fixed, so the graph's absolute paths are machine independent):
#   /src    the pinned source export, in a docker volume. Not a bind mount of the macOS
#           export: APFS is case-insensitive, so through a bind mount GeneratorCache.cpp's
#           `#include <elf.h>` finds Halide's own src/Elf.h via -I/src/src and fails.
#   /build  the docker volume buildlog-halide-linux-build; the tree is /build/cmake-linux-gcc-release
#   /raw    ../baseline/raw/linux-gcc-release (gitignored)
#   /docker this directory
# The compiler is CMake's default (/usr/bin/cc, /usr/bin/c++ = GCC 13), as the macOS
# baseline takes Apple clang by default. Halide_LLVM_ROOT names apt.llvm.org's LLVM 22
# like the macOS baseline names llvm@22. Everything else is at Halide's defaults,
# including Halide_WASM_BACKEND=wabt.
set -euo pipefail

COMPILER="${COMPILER:-gcc}"  # gcc (the image's default c++) or clang (/usr/bin/clang++, Clang 18: needs IMAGE=buildlog-linux:24.04)
VARIANT=linux-$COMPILER-release
IMAGE="${IMAGE:-buildlog-halide-linux:22}"
VOLUME=buildlog-halide-linux-build
LLVM_ROOT=/usr/lib/llvm-22
BUILD=/build/cmake-$VARIANT
ARGS=(-G Ninja -DCMAKE_BUILD_TYPE=Release "-DHalide_LLVM_ROOT=$LLVM_ROOT" -DCMAKE_EXPORT_COMPILE_COMMANDS=ON)
[[ "$COMPILER" == clang ]] && ARGS+=(-DCMAKE_C_COMPILER=/usr/bin/clang -DCMAKE_CXX_COMPILER=/usr/bin/clang++
    -DCMAKE_CXX_SCAN_FOR_MODULES=OFF) # the image has no clang-scan-deps for Clang 18: CMake would run it for every source (127)
log() { printf '== %s\n' "$*" >&2; }

if [[ $# -eq 0 ]]; then
    set -- configure build dump test normalize
fi

# ---------------------------------------------------------------------------------------
# Inside the container: one step per invocation.
if [[ "${BUILDLOG_IN_CONTAINER:-}" == 1 ]]; then
    JOBS="${JOBS:-$(nproc)}"
    NINJA_TARGETS="${NINJA_TARGETS:-all}"
    write_query() {
        local q="$1/.cmake/api/v1/query/client-buildlog"
        mkdir -p "$q"
        cp /docker/fileapi-query.json "$q/query.json"
    }
    t() { python3 -c 'import subprocess,sys,time
t=time.perf_counter(); r=subprocess.run(sys.argv[1:],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(f"{time.perf_counter()-t:.3f}"); sys.exit(r.returncode)' "$@"; }
    step="$1"
    case "$step" in
        configure)
            rm -rf "$BUILD"
            mkdir -p "$BUILD"
            write_query "$BUILD"
            start=$(date +%s.%N)
            set +e
            (cd /src && cmake "${ARGS[@]}" -B "$BUILD" -S /src) >/raw/configure.log 2>&1
            status=$?
            set -e
            echo "configure_seconds=$(python3 -c "print(round($(date +%s.%N)-$start,3))") status=$status" | tee /raw/configure-time.txt
            tail -n 5 /raw/configure.log
            exit "$status"
            ;;
        build)
            start=$(date +%s)
            set +e
            # shellcheck disable=SC2086
            ninja -C "$BUILD" -j"$JOBS" $NINJA_TARGETS >/raw/build.log 2>&1
            status=$?
            set -e
            echo "build_seconds=$(($(date +%s) - start)) jobs=$JOBS targets=$NINJA_TARGETS status=$status" |
                tee /raw/build-time.txt
            [[ $status -eq 0 ]] || { grep -E '^FAILED' /raw/build.log | head; exit "$status"; }
            ;;
        dump)
            cp "$BUILD/build.ninja" /raw/build.ninja
            mkdir -p /raw/CMakeFiles
            cp "$BUILD/CMakeFiles/rules.ninja" /raw/CMakeFiles/rules.ninja
            cp "$BUILD/CMakeCache.txt" /raw/CMakeCache.txt
            if [[ -f "$BUILD/compile_commands.json" ]]; then cp "$BUILD/compile_commands.json" /raw/; fi
            ninja -C "$BUILD" -t commands all >/raw/commands.txt
            ninja -C "$BUILD" -t targets all >/raw/targets.txt
            ninja -C "$BUILD" -t deps >/raw/deps.txt || true
            rm -rf /raw/fileapi-reply
            cp -R "$BUILD/.cmake/api/v1/reply" /raw/fileapi-reply
            ;;
        test)
            set +e
            ctest --test-dir "$BUILD" -j"$JOBS" --output-on-failure -R "$TEST_REGEX" \
                --output-junit /raw/ctest-junit.xml >/raw/ctest.log 2>&1
            status=$?
            set -e
            tail -n 45 /raw/ctest.log
            echo "ctest status=$status"
            ;;
        metrics)
            # Cold configure (empty dir), warm configure (no changes), ninja no-op; 3 runs
            # each, timed inside the container (no docker startup in the numbers).
            scratch="$BUILD-metrics"
            out=/raw/metrics.txt
            : >"$out"
            for _ in 1 2 3; do
                rm -rf "$scratch"
                mkdir -p "$scratch"
                write_query "$scratch"
                echo "cold_configure_s=$(cd /src && t cmake "${ARGS[@]}" -B "$scratch" -S /src)" >>"$out"
            done
            for _ in 1 2 3; do
                echo "warm_configure_s=$(cd /src && t cmake "${ARGS[@]}" -B "$scratch" -S /src)" >>"$out"
            done
            for _ in 1 2 3; do
                echo "ninja_noop_s=$(t ninja -C "$BUILD")" >>"$out"
            done
            ninja -C "$BUILD" -n | tail -n 1 >>"$out"
            rm -rf "$scratch"
            cat "$out"
            ;;
        *)
            echo "unknown step: $step" >&2
            exit 2
            ;;
    esac
    exit 0
fi

# ---------------------------------------------------------------------------------------
# On the host.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
BASELINE="$HERE/../baseline"
HALIDE_SRC="${HALIDE_SRC:-$HERE/../upstream}"
PIN=4e272cbfcc  # the pin of ../baseline/run-cmake.sh
SRC_VOLUME=buildlog-halide-linux-src
RAW="$BASELINE/raw/$VARIANT"
mkdir -p "$RAW"

# The source volume holds `git archive $PIN` and a marker like run-cmake.sh's exports.
want="base=$(git -C "$HALIDE_SRC" rev-parse "$PIN") patches="
have="$(docker run --rm -v "$SRC_VOLUME:/src" "$IMAGE" cat /src/.buildlog-export 2>/dev/null || true)"
if [[ "$have" != "$want" ]]; then
    log "export $PIN -> volume $SRC_VOLUME"
    git -C "$HALIDE_SRC" archive "$PIN" | docker run --rm -i -v "$SRC_VOLUME:/src" "$IMAGE" \
        sh -c "find /src -mindepth 1 -delete && tar -x -C /src && printf '%s\n' '$want' >/src/.buildlog-export"
fi

in_container() {
    docker run --rm --platform linux/arm64 \
        -v "$SRC_VOLUME:/src:ro" -v "$VOLUME:/build" -v "$RAW:/raw" -v "$HERE:/docker:ro" \
        -e BUILDLOG_IN_CONTAINER=1 -e COMPILER -e JOBS -e NINJA_TARGETS -e TEST_REGEX \
        "$IMAGE" /docker/run-cmake-linux.sh "$@"
}

for step in "$@"; do
    case "$step" in
        configure | build | dump | metrics)
            log "$step $VARIANT (in $IMAGE)"
            in_container "$step"
            ;;
        test)
            TEST_REGEX="$(cd "$REPO" && uv run --frozen python "$BASELINE/normalize.py" test-regex --scope "$BASELINE/scope.toml")"
            export TEST_REGEX
            log "ctest $VARIANT (sampled tests, in $IMAGE)"
            in_container test
            ;;
        normalize)
            log "normalize $VARIANT"
            (cd "$REPO" && uv run --frozen python "$BASELINE/normalize.py" normalize \
                --build-ninja "$RAW/build.ninja" --fileapi "$RAW/fileapi-reply" \
                --scope "$BASELINE/scope.toml" --src /src --build "$BUILD" --llvm "$LLVM_ROOT" \
                --variant "$VARIANT" -o "$BASELINE/cmake-$VARIANT.jsonl")
            ;;
        *)
            echo "unknown step: $step" >&2
            exit 2
            ;;
    esac
done
