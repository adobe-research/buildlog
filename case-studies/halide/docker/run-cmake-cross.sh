#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# M3 phase 7c: cross-build Halide with its own CMake on macOS arm64 for aarch64-linux-gnu,
# with the Linux image (linux/Dockerfile) as the sysroot, then run the sampled tests in the
# container. Captures the ground truth like ../baseline/run-cmake.sh and records every
# workaround CMake needed (README.md, "CMake effort ledger").
#
#   run-cmake-cross.sh [STEP...]
#
# STEP: setup hello host configure build dump test normalize metrics
#       (default: setup hello host configure build dump test normalize)
#
# Environment overrides:
#   HALIDE_SRC  Halide checkout whose git objects hold the pin (default ../upstream, the submodule)
#   IMAGE       (default buildlog-halide-linux:22)
#   LLVM_HOST   host LLVM (default /opt/homebrew/opt/llvm@22); also the cross compiler
#   JOBS        ninja/ctest -j (default: hw.ncpu)
#
# Layout: everything lives on a case-sensitive APFS sparse image, raw/cross.sparseimage,
# attached (-nobrowse) at ./xvol. It has to be case-sensitive: the target build compiles
# with -I$SRC/src, and on the default case-insensitive APFS GeneratorCache.cpp's
# `#include <elf.h>` then finds Halide's src/Elf.h instead of glibc's.
#   xvol/src                          `git archive 4e272cbfcc`
#   xvol/sysroot                      ./sysroot.sh of $IMAGE
#   xvol/build/cmake-cross-host       build tree 1: macOS host tools (binary2cpp,
#                                     build_halide_h, regexp_replace, all generators,
#                                     the autoscheduler plugins); exports the generators
#   xvol/build/cmake-cross-linux-aarch64  build tree 2: the Linux product
# The container mounts xvol at the same absolute path, so ctest runs the target tree as is.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
BASELINE="$HERE/../baseline"
VARIANT=cross-linux-aarch64
HALIDE_SRC="${HALIDE_SRC:-$HERE/../upstream}"
IMAGE="${IMAGE:-buildlog-halide-linux:22}"
LLVM_HOST="${LLVM_HOST:-/opt/homebrew/opt/llvm@22}"
LLD="/opt/homebrew/opt/lld@22/bin/ld.lld"
JOBS="${JOBS:-$(sysctl -n hw.ncpu)}"
PIN=4e272cbfcc
XVOL="$HERE/xvol"
IMG="$HERE/raw/cross.sparseimage"
SRC="$XVOL/src"
SYSROOT="$XVOL/sysroot"
HOST="$XVOL/build/cmake-cross-host"
BUILD="$XVOL/build/cmake-$VARIANT"
RAW="$BASELINE/raw/$VARIANT"
CROSS="$HERE/cross"
mkdir -p "$RAW" "$HERE/raw"
log() { printf '== %s\n' "$*" >&2; }
t() { python3 -c 'import subprocess,sys,time
t=time.perf_counter(); r=subprocess.run(sys.argv[1:],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(f"{time.perf_counter()-t:.3f}"); sys.exit(r.returncode)' "$@"; }

# Tree 1. The macOS preset, as the macOS baseline, and only the targets tree 2 runs.
HOST_ARGS=(--preset macOS "-DHalide_LLVM_ROOT=$LLVM_HOST")
HOST_TARGETS=(Halide-halide_generators binary2cpp build_halide_h regexp_replace
    Halide_Adams2019 Halide_Anderson2021 Halide_Li2018 Halide_Mullapudi2016)

# Tree 2. Each line after the first three is a workaround; README.md explains them.
target_args() { # target_args BUILD_DIR
    local emu=("$CROSS/host-twin.py" --sysroot "$SYSROOT" --host-build "$HOST" --target-build "$1"
        --host-bin "$LLVM_HOST/bin" --host-bin /opt/homebrew/bin --)
    local IFS=';'
    TARGET_ARGS=(-G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
        "-DCMAKE_TOOLCHAIN_FILE=$CROSS/toolchain.linux-aarch64-clang.cmake" "-DCMAKE_SYSROOT=$SYSROOT"
        "-DCMAKE_FIND_ROOT_PATH=$SRC/dependencies"
        -DWITH_PYTHON_BINDINGS=OFF
        -DWITH_TUTORIALS=OFF
        "-DCMAKE_CROSSCOMPILING_EMULATOR=${emu[*]}"
        "-DFlatBuffers_DIR=$CROSS/pkg/flatbuffers"
        "-DHalide-halide_generators_DIR=$CROSS/pkg/Halide-halide_generators" "-DHALIDE_CROSS_HOST_BUILD=$HOST")
}
write_query() {
    local q="$1/.cmake/api/v1/query/client-buildlog"
    mkdir -p "$q"
    cp "$HERE/fileapi-query.json" "$q/query.json"
}
need_xvol() { mount | grep -q " on $XVOL " || { echo "xvol not attached; run the setup step" >&2; exit 1; }; }

if [[ $# -eq 0 ]]; then
    set -- setup hello host configure build dump test normalize
fi

for step in "$@"; do
    case "$step" in
        setup)
            if [[ ! -e "$IMG" ]]; then
                log "create $IMG (case-sensitive APFS, sparse, 60 GB max)"
                hdiutil create -quiet -size 60g -type SPARSE -fs "Case-sensitive APFS" -volname halide-xvol "$IMG"
            fi
            if ! mount | grep -q " on $XVOL "; then
                mkdir -p "$XVOL"
                hdiutil attach -quiet -nobrowse -mountpoint "$XVOL" "$IMG"
            fi
            want="base=$(git -C "$HALIDE_SRC" rev-parse "$PIN") patches="
            if [[ "$(cat "$SRC/.buildlog-export" 2>/dev/null)" != "$want" ]]; then
                log "export $PIN -> $SRC"
                rm -rf "$SRC"
                mkdir -p "$SRC"
                git -C "$HALIDE_SRC" archive "$PIN" | tar -x -C "$SRC"
                printf '%s\n' "$want" >"$SRC/.buildlog-export"
            fi
            want="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
            if [[ "$(cat "$SYSROOT/.buildlog-image" 2>/dev/null)" != "$want" ]]; then
                "$HERE/sysroot.sh" "$SYSROOT"
                printf '%s\n' "$want" >"$SYSROOT/.buildlog-image"
            fi
            ;;
        hello)
            need_xvol
            log "hello: cross-compile cross/hello.cpp, run it in $IMAGE"
            out="$XVOL/build/hello"
            mkdir -p "$XVOL/build"
            "$LLVM_HOST/bin/clang++" --target=aarch64-linux-gnu "--sysroot=$SYSROOT" -fuse-ld=lld \
                "--ld-path=$LLD" -O2 -pthread "$CROSS/hello.cpp" -o "$out"
            {
                file "$out" | sed "s#$XVOL#\$XVOL#"
                docker run --rm --platform linux/arm64 -v "$XVOL:$XVOL:ro" "$IMAGE" "$out"
            } | tee "$RAW/hello.txt"
            ;;
        host)
            need_xvol
            log "host tree: configure + build ${HOST_TARGETS[*]}"
            rm -rf "$HOST"
            s=$(python3 -c 'import time; print(time.time())')
            (cd "$SRC" && cmake "${HOST_ARGS[@]}" -B "$HOST" -S "$SRC") >"$RAW/host-configure.log" 2>&1
            m=$(python3 -c 'import time; print(time.time())')
            ninja -C "$HOST" -j"$JOBS" "${HOST_TARGETS[@]}" >"$RAW/host-build.log" 2>&1
            e=$(python3 -c 'import time; print(time.time())')
            python3 -c "print(f'host_configure_s={$m-$s:.3f} host_build_s={$e-$m:.1f} jobs=$JOBS')" |
                tee "$RAW/host-time.txt"
            ;;
        configure)
            need_xvol
            log "target tree: configure"
            rm -rf "$BUILD"
            mkdir -p "$BUILD"
            write_query "$BUILD"
            target_args "$BUILD"
            printf '%s\n' "${TARGET_ARGS[@]}" | sed "s#$XVOL#\$XVOL#g; s#$HERE#\$DOCKER#g" >"$RAW/configure-args.txt"
            set +e
            s=$(python3 -c 'import time; print(time.time())')
            (cd "$SRC" && cmake "${TARGET_ARGS[@]}" -B "$BUILD" -S "$SRC") >"$RAW/configure.log" 2>&1
            status=$?
            e=$(python3 -c 'import time; print(time.time())')
            set -e
            python3 -c "print(f'configure_seconds={$e-$s:.3f} status=$status')" | tee "$RAW/configure-time.txt"
            tail -n 3 "$RAW/configure.log"
            exit_on=$status
            [[ $exit_on -eq 0 ]] || exit "$exit_on"
            ;;
        build)
            need_xvol
            log "target tree: build all"
            s=$(date +%s)
            set +e
            ninja -C "$BUILD" -j"$JOBS" -k 0 >"$RAW/build.log" 2>&1
            status=$?
            set -e
            echo "build_seconds=$(($(date +%s) - s)) jobs=$JOBS status=$status load=$(sysctl -n vm.loadavg)" |
                tee "$RAW/build-time.txt"
            [[ $status -eq 0 ]] || { grep -E '^FAILED' "$RAW/build.log" | head; exit "$status"; }
            ;;
        dump)
            need_xvol
            cp "$BUILD/build.ninja" "$RAW/build.ninja"
            mkdir -p "$RAW/CMakeFiles"
            cp "$BUILD/CMakeFiles/rules.ninja" "$RAW/CMakeFiles/rules.ninja"
            cp "$BUILD/CMakeCache.txt" "$RAW/CMakeCache.txt"
            cp "$BUILD/compile_commands.json" "$RAW/"
            ninja -C "$BUILD" -t commands all >"$RAW/commands.txt"
            ninja -C "$BUILD" -t targets all >"$RAW/targets.txt"
            ninja -C "$BUILD" -t deps >"$RAW/deps.txt" || true
            rm -rf "$RAW/fileapi-reply"
            cp -R "$BUILD/.cmake/api/v1/reply" "$RAW/fileapi-reply"
            cp "$HOST/build.ninja" "$RAW/host-build.ninja"
            ;;
        test)
            need_xvol
            regex="$(cd "$REPO" && uv run --frozen python "$BASELINE/normalize.py" test-regex --scope "$BASELINE/scope.toml")"
            log "ctest $VARIANT (sampled tests, in $IMAGE)"
            set +e
            # The target tree's test commands name host-twin.py (the emulator), which
            # execs the Linux program unchanged on Linux; hence the cross/ mount.
            docker run --rm --platform linux/arm64 -v "$XVOL:$XVOL" -v "$CROSS:$CROSS:ro" "$IMAGE" \
                ctest --test-dir "$BUILD" -j"$JOBS" --output-on-failure -R "$regex" \
                --output-junit "$BUILD/ctest-junit.xml" >"$RAW/ctest.log" 2>&1
            status=$?
            set -e
            cp "$BUILD/ctest-junit.xml" "$RAW/" 2>/dev/null || true
            tail -n 20 "$RAW/ctest.log"
            echo "ctest status=$status"
            ;;
        normalize)
            log "normalize $VARIANT"
            (cd "$REPO" && uv run --frozen python "$BASELINE/normalize.py" normalize \
                --build-ninja "$RAW/build.ninja" --fileapi "$RAW/fileapi-reply" \
                --scope "$BASELINE/scope.toml" --src "$SRC" --build "$BUILD" \
                --llvm "$SYSROOT/usr/lib/llvm-22" --root "\$SYSROOT=$SYSROOT" \
                --root "\$HOST_BUILD=$HOST" --root "\$DOCKER=$HERE" \
                --variant "$VARIANT" -o "$BASELINE/cmake-$VARIANT.jsonl")
            ;;
        metrics)
            need_xvol
            # Cold/warm configure of tree 2 and a ninja no-op, 3 runs each, like run-cmake.sh;
            # plus a cold configure of tree 1 (the cross flow needs both).
            out="$RAW/metrics.txt"
            : >"$out"
            scratch="$BUILD-metrics"
            target_args "$BUILD"
            for _ in 1 2 3; do
                rm -rf "$scratch"
                mkdir -p "$scratch"
                write_query "$scratch"
                echo "cold_configure_s=$(cd "$SRC" && t cmake "${TARGET_ARGS[@]}" -B "$scratch" -S "$SRC")" >>"$out"
            done
            for _ in 1 2 3; do
                echo "warm_configure_s=$(cd "$SRC" && t cmake "${TARGET_ARGS[@]}" -B "$scratch" -S "$SRC")" >>"$out"
            done
            for _ in 1 2 3; do
                echo "ninja_noop_s=$(t ninja -C "$BUILD")" >>"$out"
            done
            ninja -C "$BUILD" -n | tail -n 1 >>"$out"
            for _ in 1 2 3; do
                rm -rf "$scratch"
                echo "host_cold_configure_s=$(cd "$SRC" && t cmake "${HOST_ARGS[@]}" -B "$scratch" -S "$SRC")" >>"$out"
            done
            rm -rf "$scratch"
            echo "load=$(sysctl -n vm.loadavg)" >>"$out"
            cat "$out"
            ;;
        *)
            echo "unknown step: $step" >&2
            exit 2
            ;;
    esac
done
