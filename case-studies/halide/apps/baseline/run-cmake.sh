#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Build the two apps with their own CMake builds and record the commands
# (compact JSON, see commands.py) that case studies compare against.
#
#   run-cmake.sh wasm|android
#
# Environment: HALIDE_PREFIX (an installed Halide), EMSDK (wasm), EM_BINARYEN_ROOT
# (wasm: a binaryen newer than emsdk's bundled one), ANDROID_NDK_ROOT (android),
# HALIDE_SRC (default ../../upstream), BUILD (default <repo>/build/halide-apps-cmake).
# On Linux (linux/run.sh baselines) the records go to linux/ (the image's GCC) or
# linux-clang/ (BASELINE_FAMILY=clang) next to this script, and the apps are built
# in the image (/opt/halide, /opt/emsdk, /opt/android-ndk).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
SRC="$(cd "${HALIDE_SRC:-$HERE/../../upstream}" && pwd)"
BUILD="${BUILD:-$REPO/build/halide-apps-cmake}"
export CMAKE_GENERATOR=Ninja
OUT="$HERE"
if [[ "$(uname -s)" == Linux ]]; then
  OUT="$HERE/linux"
  if [[ "${BASELINE_FAMILY:-gcc}" == clang ]]; then
    OUT="$HERE/linux-clang"
    export CC=clang CXX=clang++
  fi
  mkdir -p "$OUT"
fi
mkdir -p "$BUILD"
case "$1" in
wasm)
  # The super-build: ExternalProject builds generator/ for the host and app/ under Emscripten.
  cmake -S "$SRC/apps/HelloWasm" -B "$BUILD/wasm" -DCMAKE_PREFIX_PATH="$HALIDE_PREFIX" \
    -DEMSCRIPTEN_TOOLCHAIN_FILE="$EMSDK/upstream/emscripten/cmake/Modules/Platform/Emscripten.cmake"
  cmake --build "$BUILD/wasm"
  python3 "$HERE/commands.py" extract --root "PREFIX=$HALIDE_PREFIX" --root "EMSDK=$EMSDK" \
    --root "SRC=$SRC/apps/HelloWasm" --root "BUILD=$BUILD/wasm" -o "$OUT/cmake-hellowasm.json" \
    "$BUILD/wasm/generator" "$BUILD/wasm/app"
  ;;
android)
  # What Gradle runs per ABI: the NDK toolchain over app/src/main/cpp, minSdk 23, which also
  # configures and builds the host generators itself. Gradle's own build type is not
  # reproduced (no SDK/JRE needed here), so Release.
  cp -R "$SRC/apps/HelloAndroid" "$BUILD/src"
  cpp="$BUILD/src/app/src/main/cpp"
  for abi in arm64-v8a x86_64; do
    cmake -S "$cpp" -B "$BUILD/$abi" -DCMAKE_TOOLCHAIN_FILE="$ANDROID_NDK_ROOT/build/cmake/android.toolchain.cmake" \
      -DANDROID_ABI=$abi -DANDROID_PLATFORM=android-23 -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$HALIDE_PREFIX"
    cmake --build "$BUILD/$abi"
  done
  python3 "$HERE/commands.py" extract --root "NDK=$ANDROID_NDK_ROOT" --root "PREFIX=$HALIDE_PREFIX" \
    --root "SRC=$BUILD/src" --root "BUILD=$BUILD" -o "$OUT/cmake-helloandroid.json" \
    "$BUILD/arm64-v8a" "$BUILD/x86_64"
  ;;
*) echo "usage: $0 wasm|android" >&2; exit 2 ;;
esac
