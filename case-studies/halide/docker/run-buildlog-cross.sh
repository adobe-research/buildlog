#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# M3 phase 7c, buildlog's side: configure, build and test libHalide for aarch64-linux-gnu on
# macOS arm64, in one build tree, with no annotation (README.md; ../libhalide/presets/
# cross-linux-aarch64.toml). The CMake side is run-cmake-cross.sh, whose `setup` step this
# script runs when the volume is absent: the case-sensitive volume (xvol), the Halide export
# (xvol/src) and the sysroot of the 7b image (xvol/sysroot).
#
#   run-buildlog-cross.sh [STEP...]      STEP: setup configure build test (default: setup configure test)
#
# Environment: JOBS (default hw.ncpu), IMAGE (buildlog-halide-linux:22), OUT (a directory for
# the logs and timings; default build/halide-cross), BUILDLOG_SOUFFLE (the
# patched Soufflé, as for every buildlog run). Needs Docker, Homebrew llvm@22 and lld@22.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
XVOL="$HERE/xvol"
BUILD="$XVOL/build/buildlog-cross"
WORK="$XVOL/work/libhalide"
OUT="${OUT:-$REPO/build/halide-cross}"
JOBS="${JOBS:-$(sysctl -n hw.ncpu)}"
STEPS=("$@")
[[ ${#STEPS[@]} -gt 0 ]] || STEPS=(setup configure test)
mkdir -p "$OUT"
export BUILDLOG_LLVM_BIN="${BUILDLOG_LLVM_BIN:-/opt/homebrew/opt/llvm@22/bin}"
BL=(uv run --project "$REPO" --frozen buildlog)

timed() { # timed NAME CMD...: the command's log and seconds go to $OUT
    local name=$1
    shift
    local t0=$SECONDS
    "$@" >"$OUT/$name.log" 2>&1 || {
        tail -30 "$OUT/$name.log" >&2
        return 1
    }
    echo "$((SECONDS - t0))" >"$OUT/$name.seconds"
    printf '%s: %s s\n' "$name" "$((SECONDS - t0))" >&2
}

for step in "${STEPS[@]}"; do
    case "$step" in
    setup)
        [[ -d "$XVOL/sysroot/usr" && -d "$XVOL/src/src" ]] || "$HERE/run-cmake-cross.sh" setup
        ;;
    configure)
        # A copy of the project without its lock: a lock records one preset's packages.
        rm -rf "$WORK" "$BUILD"
        mkdir -p "$WORK" "$XVOL/build"
        rsync -a --exclude buildlog.lock "$REPO/case-studies/halide/libhalide/" "$WORK/"
        timed buildlog-configure "${BL[@]}" configure "$WORK" -C "$BUILD" \
            --package-path "$REPO/case-studies/halide/packages" \
            --user "$REPO/case-studies/halide/libhalide/presets/cross-linux-aarch64.toml" \
            -D "HALIDE_SRC=$XVOL/src" -D "SYSROOT=$XVOL/sysroot"
        ;;
    build)
        timed buildlog-build ninja -C "$BUILD/souffle" -j "$JOBS"
        ;;
    test)
        # Builds the tests' files first, then runs them in the container. The scope is the
        # CMake cross run's (WITH_TUTORIALS=OFF: the tutorial generators have no host twin),
        # so the build step is not a default step: `all` also builds the tutorials.
        local_regex="$(cd "$REPO" && uv run --frozen python "$REPO/case-studies/halide/baseline/normalize.py" test-regex --scope "$REPO/case-studies/halide/baseline/scope.toml")"
        timed buildlog-test "${BL[@]}" test -C "$BUILD" -R "$local_regex" -j "${TEST_JOBS:-6}" --output-on-failure \
            --json "$OUT/buildlog-test.json"
        tail -1 "$OUT/buildlog-test.log"
        ;;
    *)
        echo "unknown step $step" >&2
        exit 2
        ;;
    esac
done
