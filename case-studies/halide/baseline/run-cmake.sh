#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Configure/build Halide with its own CMake build and capture the ground truth
# for the buildlog M3 case study.
#
#   run-cmake.sh [VARIANT] [STEP...]
#
# VARIANT: release (default) | debug | asan | universal2 | x86_64
# STEP:    configure build dump test normalize install metrics
#          (default: all but install for release; configure dump normalize for the others)
#
# Environment overrides:
#   HALIDE_SRC   Halide checkout (default ../upstream, the submodule; the pin is main's 4e272cbfcc).
#                Only its git objects are used: every variant builds from a `git archive`
#                export under $HALIDE_BUILD_ROOT (see export_tree), never the worktree.
#   HALIDE_BUILD_ROOT  build trees and source exports (default <repo>/build/case-halide)
#   LLVM_ROOT    LLVM prefix (default /opt/homebrew/opt/llvm@22)
#   JOBS         ninja -j (default: hw.ncpu)
#   NINJA_TARGETS  targets for the build step (default: all)
#
# Build trees go to $HALIDE_BUILD_ROOT/cmake-$VARIANT and source exports to
# $HALIDE_BUILD_ROOT/src-* (the repository's build/ is gitignored). Raw artifacts go to
# baseline/raw/$VARIANT (gitignored here).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
HALIDE_SRC="${HALIDE_SRC:-$HERE/../upstream}"
HALIDE_BUILD_ROOT="${HALIDE_BUILD_ROOT:-$REPO/build/case-halide}"
LLVM_ROOT="${LLVM_ROOT:-/opt/homebrew/opt/llvm@22}"
JOBS="${JOBS:-$(sysctl -n hw.ncpu)}"
NINJA_TARGETS="${NINJA_TARGETS:-all}"

VARIANT="${1:-release}"
shift || true
if [[ $# -eq 0 ]]; then
    if [[ "$VARIANT" == release ]]; then
        set -- configure build dump test normalize
    else
        set -- configure dump normalize
    fi
fi

BUILD="$HALIDE_BUILD_ROOT/cmake-$VARIANT"
RAW="$HERE/raw/$VARIANT"

# The pin: Halide main at 4e272cbfcc (2026-10-13, PR #9548). It contains PR #9473 (the wabt 1.0.42
# fix, so the default Halide_WASM_BACKEND=wabt builds against Homebrew's wabt) and PR #9469
# (add_halide_library for universal2), which the earlier pin (35b7aa604e) needed as patches.
PIN=4e272cbfcc

# export_tree NAME BASE [COMMIT...]: `git archive BASE` into $HALIDE_BUILD_ROOT/NAME, then
# apply each COMMIT's own diff with patch(1). (Not `git apply`: inside the worktree's
# build/ it would resolve paths against the worktree and silently skip files.) A marker
# file records what the export holds, and a mismatching export is rebuilt.
export_tree() {
    local dir="$HALIDE_BUILD_ROOT/$1" base="$2" c want
    shift 2
    want="base=$(git -C "$HALIDE_SRC" rev-parse "$base") patches=$(
        for c in "$@"; do git -C "$HALIDE_SRC" rev-parse "$c"; done | tr '\n' ' '
    )"
    if [[ ! -f "$dir/.buildlog-export" || "$(cat "$dir/.buildlog-export")" != "$want" ]]; then
        log "export $base + [$*] -> $dir"
        rm -rf "$dir"
        mkdir -p "$dir"
        git -C "$HALIDE_SRC" archive "$base" | tar -x -C "$dir"
        for c in "$@"; do
            git -C "$HALIDE_SRC" diff "$c^" "$c" | patch -s -p1 --forward -d "$dir"
        done
        echo "$want" >"$dir/.buildlog-export"
    fi
    SRC="$dir"
}

mkdir -p "$RAW"
log() { printf '== %s\n' "$*" >&2; }

# The macOS preset: Ninja, CMAKE_BUILD_TYPE=Release, Halide_LLVM_ROOT (overridden to llvm@22).
# Everything else is at its default, including Halide_WASM_BACKEND=wabt.
ARGS=(--preset macOS "-DHalide_LLVM_ROOT=$LLVM_ROOT" -DCMAKE_EXPORT_COMPILE_COMMANDS=ON)
export_tree "src-$PIN" "$PIN"
case "$VARIANT" in
    release) ;;
    debug) ARGS+=(-DCMAKE_BUILD_TYPE=Debug) ;;
    asan)
        # Adapted from cmake/toolchain.linux-x64-asan.cmake: -fsanitize=address on
        # C/C++ (CMake also passes CMAKE_<LANG>_FLAGS to link lines), no fuzz tests.
        # We keep Apple clang (it ships a matching ASan runtime) and Release, so the
        # graph differs from `release` only by the sanitizer. See scope.md.
        ARGS+=("-DCMAKE_C_FLAGS=-fsanitize=address" "-DCMAKE_CXX_FLAGS=-fsanitize=address"
            -DWITH_TEST_FUZZ=OFF)
        ;;
    universal2) ARGS+=("-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64") ;;
    x86_64) ARGS+=(-DCMAKE_OSX_ARCHITECTURES=x86_64) ;;
    *)
        echo "unknown variant: $VARIANT" >&2
        exit 2
        ;;
esac

write_query() {
    local q="$BUILD/.cmake/api/v1/query/client-buildlog"
    mkdir -p "$q"
    cat >"$q/query.json" <<'EOF'
{
  "requests": [
    {"kind": "codemodel", "version": 2},
    {"kind": "toolchains", "version": 1},
    {"kind": "cmakeFiles", "version": 1},
    {"kind": "cache", "version": 2}
  ]
}
EOF
}

for step in "$@"; do
    case "$step" in
        configure)
            log "configure $VARIANT -> $BUILD"
            mkdir -p "$BUILD"
            write_query
            set +e
            (cd "$SRC" && cmake "${ARGS[@]}" -B "$BUILD" -S "$SRC") >"$RAW/configure.log" 2>&1
            status=$?
            set -e
            tail -n 5 "$RAW/configure.log" >&2
            [[ $status -eq 0 ]] || { echo "configure failed ($status), see $RAW/configure.log" >&2; exit "$status"; }
            ;;
        build)
            log "build $VARIANT (ninja -j$JOBS $NINJA_TARGETS)"
            start=$(date +%s)
            set +e
            # shellcheck disable=SC2086
            ninja -C "$BUILD" -j"$JOBS" $NINJA_TARGETS >"$RAW/build.log" 2>&1
            status=$?
            set -e
            end=$(date +%s)
            echo "build_seconds=$((end - start)) jobs=$JOBS targets=$NINJA_TARGETS status=$status" |
                tee "$RAW/build-time.txt"
            [[ $status -eq 0 ]] || { grep -E '^FAILED' "$RAW/build.log" | head >&2; exit "$status"; }
            ;;
        dump)
            log "dump raw artifacts -> $RAW"
            cp "$BUILD/build.ninja" "$RAW/build.ninja"
            mkdir -p "$RAW/CMakeFiles"
            cp "$BUILD/CMakeFiles/rules.ninja" "$RAW/CMakeFiles/rules.ninja"
            cp "$BUILD/CMakeCache.txt" "$RAW/CMakeCache.txt"
            if [[ -f "$BUILD/compile_commands.json" ]]; then cp "$BUILD/compile_commands.json" "$RAW/"; fi
            ninja -C "$BUILD" -t commands all >"$RAW/commands.txt"
            ninja -C "$BUILD" -t targets all >"$RAW/targets.txt"
            ninja -C "$BUILD" -t deps >"$RAW/deps.txt" || true
            rm -rf "$RAW/fileapi-reply"
            cp -R "$BUILD/.cmake/api/v1/reply" "$RAW/fileapi-reply"
            ;;
        test)
            regex="$(cd "$REPO" && uv run --frozen python "$HERE/normalize.py" test-regex --scope "$HERE/scope.toml")"
            log "ctest $VARIANT (sampled tests)"
            set +e
            ctest --test-dir "$BUILD" -j"$JOBS" --output-on-failure -R "$regex" \
                --output-junit "$RAW/ctest-junit.xml" >"$RAW/ctest.log" 2>&1
            status=$?
            set -e
            tail -n 15 "$RAW/ctest.log" >&2
            echo "ctest status=$status" >&2
            ;;
        normalize)
            log "normalize $VARIANT"
            (cd "$REPO" && uv run --frozen python "$HERE/normalize.py" normalize \
                --build-ninja "$RAW/build.ninja" --fileapi "$RAW/fileapi-reply" \
                --scope "$HERE/scope.toml" --src "$SRC" --build "$BUILD" --llvm "$LLVM_ROOT" \
                --variant "$VARIANT" -o "$HERE/cmake-$VARIANT.jsonl")
            ;;
        install)
            prefix="$BUILD-install"
            log "install $VARIANT -> $prefix"
            rm -rf "$prefix"
            cmake --install "$BUILD" --prefix "$prefix" >"$RAW/install.log" 2>&1
            (cd "$REPO" && uv run --frozen python "$HERE/normalize.py" install-manifest \
                --prefix "$prefix" --src "$SRC" --build "$BUILD" --llvm "$LLVM_ROOT" \
                -o "$HERE/cmake-$VARIANT-install.jsonl")
            ;;
        metrics)
            # Cold configure (empty dir), warm configure (no changes), ninja no-op; 3 runs each.
            # Cold/warm use a scratch tree next to $BUILD so the built tree is not disturbed;
            # the no-op build runs in the built $BUILD.
            scratch="$BUILD-metrics"
            out="$RAW/metrics.txt"
            : >"$out"
            t() { python3 -c 'import subprocess,sys,time
t=time.perf_counter(); r=subprocess.run(sys.argv[1:],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(f"{time.perf_counter()-t:.3f}"); sys.exit(r.returncode)' "$@"; }
            for i in 1 2 3; do
                rm -rf "$scratch"
                mkdir -p "$scratch"
                BUILD="$scratch" write_query
                echo "cold_configure_s=$(cd "$SRC" && t cmake "${ARGS[@]}" -B "$scratch" -S "$SRC")" >>"$out"
            done
            for i in 1 2 3; do
                echo "warm_configure_s=$(cd "$SRC" && t cmake "${ARGS[@]}" -B "$scratch" -S "$SRC")" >>"$out"
            done
            for i in 1 2 3; do
                echo "ninja_noop_s=$(t ninja -C "$BUILD")" >>"$out"
            done
            ninja -C "$BUILD" -n | tail -n 1 >>"$out"
            rm -rf "$scratch"
            cat "$out" >&2
            ;;
        *)
            echo "unknown step: $step" >&2
            exit 2
            ;;
    esac
done
