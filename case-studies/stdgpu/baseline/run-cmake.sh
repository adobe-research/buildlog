#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The stdgpu case study's ground truth: stdgpu's own CMake build (Release,
# Ninja, CUDA 13.4 for arm64 Linux, architecture 80), compile and link only.
# Run in the CUDA image (linux/cuda/Dockerfile): `IMAGE=buildlog-cuda:13.4
# linux/run.sh exec gcc case-studies/stdgpu/baseline/run-cmake.sh`.
#
# The committed files are normalized: the source and build directories are
# replaced by $SRC and $BUILD. Raw trees go to baseline/linux/raw (gitignored).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "${STDGPU_SRC:-$HERE/../upstream}" && pwd)"
OUT="$HERE/linux"
RAW="${BASELINE_RAW:-$OUT/raw}"
BUILD="$RAW/cmake-build"
PIN=503c63af2dcdfac4dfeb36c261a5dd37466a5f55
git() { command git -c safe.directory="$SRC" "$@"; }

if [[ "$(git -C "$SRC" rev-parse HEAD)" != "$PIN" ]]; then
    echo "run-cmake.sh: $SRC is not at $PIN" >&2
    exit 1
fi
rm -rf "$BUILD"
mkdir -p "$BUILD" "$OUT"
norm() { sed -e "s#$BUILD#\$BUILD#g" -e "s#$SRC#\$SRC#g"; }

{
    echo "# Environment"
    uname -sm
    . /etc/os-release && echo "$PRETTY_NAME"
    cmake --version | head -1
    echo "ninja $(ninja --version)"
    c++ --version | head -1
    nvcc --version | tail -2
    echo "stdgpu $(git -C "$SRC" rev-parse HEAD)"
} | norm > "$OUT/environment.txt"

cmake -S "$SRC" -B "$BUILD" -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CUDA_ARCHITECTURES=80 -DSTDGPU_BUILD_TESTS=OFF -DSTDGPU_BUILD_BENCHMARKS=OFF \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON > "$RAW/configure.log" 2>&1
ninja -C "$BUILD" > "$RAW/build.log" 2>&1
ninja -C "$BUILD" -t commands | norm > "$OUT/commands.txt"
norm < "$BUILD/src/stdgpu/include/stdgpu/config.h" > "$OUT/config.h"
echo "baseline: $(ninja -C "$BUILD" -t commands | wc -l) commands"
