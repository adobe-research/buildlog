#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The SPIRV-Tools case study's ground truth: SPIRV-Tools v2026.3's own CMake
# build (Release, Ninja, native arm64) against SPIRV-Headers installed as a
# package, its GTest suite, and the artifacts the buildlog build is compared with.
#
#   run-cmake.sh
#
# Environment overrides:
#   SPIRV_TOOLS_SRC   the SPIRV-Tools checkout (default ../upstream, the submodule,
#                     at v2026.3 = b707790a), with googletest, effcee, re2 and abseil_cpp
#                     under external/ at the DEPS revisions (../fetch-deps.sh). Only read.
#   SPIRV_HEADERS     the SPIRV-Headers install prefix (required: the install of SPIRV-Headers
#                     29981f65 = vulkan-sdk-1.4.357.0; see ../README.md).
#   WORK              where the build tree goes (default <repo>/build/case-spirv-tools/cmake-build).
#   PYTHON            the interpreter (default /opt/homebrew/bin/python3).
#   JOBS              build parallelism (default 4).
#
# On Linux (linux/run.sh baselines) the files go to baseline/linux (the image's GCC) or
# baseline/linux-clang (BASELINE_FAMILY=clang, Clang 18); the interpreter is
# /usr/bin/python3, SPIRV_HEADERS is /opt/spirv-headers, and ELF's readelf and nm -D
# replace otool and nm -gU, sha256sum shasum.
#
# The committed files next to this script are normalized: the source, build and
# SPIRV-Headers dirs are replaced by $SRC, $BUILD and $HEADERS.
set -euo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
SRC="$(cd "${SPIRV_TOOLS_SRC:-$HERE/../upstream}" && pwd)"
[[ "$(uname -s)" == Linux ]] && SPIRV_HEADERS="${SPIRV_HEADERS:-/opt/spirv-headers}"
: "${SPIRV_HEADERS:?run-cmake.sh: set SPIRV_HEADERS to the SPIRV-Headers install prefix (see ../README.md)}"
HEADERS="$(cd "$SPIRV_HEADERS" && pwd)"
BUILD="${WORK:-$REPO/build/case-spirv-tools/cmake-build}"
OS="$(uname -s)"
OUT="$HERE"
SHA=(shasum -a 256)
DEFPY=/opt/homebrew/bin/python3
if [[ "$OS" == Linux ]]; then
    OUT="$HERE/linux"
    [[ "${BASELINE_FAMILY:-gcc}" == clang ]] && { OUT="$HERE/linux-clang"; export CC=clang CXX=clang++; }
    mkdir -p "$OUT"
    SHA=(sha256sum)
    DEFPY=/usr/bin/python3
fi
PYTHON="${PYTHON:-$DEFPY}"
JOBS="${JOBS:-4}"
PIN=b707790a898e44038547df54580022fc1cf89c3d

if [[ "$(git -C "$SRC" rev-parse HEAD)" != "$PIN" ]]; then
    echo "run-cmake.sh: $SRC is not at v2026.3 ($PIN)" >&2
    exit 1
fi
if [[ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]]; then
    echo "run-cmake.sh: $SRC has local changes" >&2
    exit 1
fi
if [[ -e "$SRC/external/spirv-headers" ]]; then
    echo "run-cmake.sh: $SRC/external/spirv-headers exists; this baseline imports an install" >&2
    exit 1
fi

rm -rf "$BUILD"
mkdir -p "$BUILD"

norm() { sed -e "s#$BUILD#\$BUILD#g" -e "s#$HEADERS#\$HEADERS#g" -e "s#$SRC#\$SRC#g"; }

{
    echo "# Environment"
    uname -sm
    if [[ "$OS" == Darwin ]]; then sw_vers -productVersion; else . /etc/os-release && echo "$PRETTY_NAME"; fi
    cmake --version | head -1
    echo "ninja $(ninja --version)"
    ${CXX:-c++} --version | head -1
    "$PYTHON" --version
    echo "SPIRV-Tools $(git -C "$SRC" describe --tags) $(git -C "$SRC" rev-parse HEAD)"
    for d in googletest effcee re2 abseil_cpp; do
        echo "external/$d $(git -C "$SRC/external/$d" rev-parse HEAD)"
    done
    echo "SPIRV-Headers install: \$HEADERS (see README for its commit)"
} > "$BUILD/environment.txt"

{
    cmake -S "$SRC" -B "$BUILD" -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
        -DSPIRV-Headers_SOURCE_DIR="$HEADERS" \
        -DPython3_EXECUTABLE="$PYTHON"
    cmake --build "$BUILD" -j "$JOBS" --verbose
    ctest --test-dir "$BUILD" -j "$JOBS" --output-on-failure
} > "$BUILD/cmake-run.log" 2>&1 || { echo "see $BUILD/cmake-run.log" >&2; exit 1; }

cp "$BUILD/environment.txt" "$OUT/environment.txt"
ninja -C "$BUILD" -t commands | norm > "$BUILD/commands-full.txt"
# The in-scope targets' commands (summarized) and the generator commands
# (Python over SPIRV-Headers' grammar), and what they wrote.
python3 "$HERE/summarize_commands.py" < "$BUILD/commands-full.txt" > "$OUT/commands.txt"
# (Linux: cmake itself lives under a python dist-packages directory.)
if [[ "$OS" == Linux ]]; then
    grep -F "$PYTHON " "$BUILD/commands-full.txt" > "$OUT/generators.txt" || true
else
    grep -E 'python|Python' "$BUILD/commands-full.txt" > "$OUT/generators.txt" || true
fi
{
    for f in core_tables_body.inc core_tables_header.inc generators.inc DebugInfo.h \
        OpenCLDebugInfo100.h build-version.inc spvasm.vim; do
        [[ -f "$BUILD/$f" ]] && printf '%s  %s\n' "$("${SHA[@]}" "$BUILD/$f" | cut -d' ' -f1)" "$f"
    done
    for f in "$BUILD"/source/opt/*.inc "$BUILD"/source/*.inc; do
        [[ -f "$f" ]] && printf '%s  %s\n' "$("${SHA[@]}" "$f" | cut -d' ' -f1)" "${f#$BUILD/}"
    done
} > "$OUT/generated-sha256.txt"
grep -E '^(SPIRV_|ENABLE_|SKIP_|Python3_EXECUTABLE|CMAKE_BUILD_TYPE|BUILD_SHARED)' \
    "$BUILD/CMakeCache.txt" | sort | norm > "$OUT/options.txt"
# The shared library's exports in full; the static libraries' defined
# external symbols as a count and a digest of the sorted list.
SHLIB=libSPIRV-Tools-shared.dylib
NMX=(nm -gU)
if [[ "$OS" == Linux ]]; then SHLIB=libSPIRV-Tools-shared.so; NMX=(nm -g --defined-only); fi
{
    echo "===== source/$SHLIB"
    "${NMX[@]}" "$BUILD/source/$SHLIB" | awk '{print $NF}' | sort -u
    for f in source/libSPIRV-Tools.a source/opt/libSPIRV-Tools-opt.a; do
        syms=$("${NMX[@]}" "$BUILD/$f" | grep -v -e '^$' -e ':$' | awk '{print $NF}' | sort -u)
        echo "===== $f: $(printf '%s\n' "$syms" | wc -l | tr -d ' ') symbols, sha256 $(printf '%s\n' "$syms" | "${SHA[@]}" | cut -d' ' -f1)"
    done
} > "$OUT/exported-symbols.txt"
if [[ "$OS" == Linux ]]; then
    {
        echo "===== readelf -d source/$SHLIB"
        readelf -d "$BUILD/source/$SHLIB" | grep -E 'NEEDED|SONAME|RUNPATH|RPATH' | awk '{$1=""; print substr($0,2)}'
        for exe in spirv-as spirv-dis spirv-val spirv-opt; do
            echo "===== readelf -d tools/$exe"
            readelf -d "$BUILD/tools/$exe" | grep -E 'NEEDED|RUNPATH|RPATH' | awk '{$1=""; print substr($0,2)}'
        done
    } | norm > "$OUT/elf-metadata.txt"
else
{
    echo "===== otool -L source/libSPIRV-Tools-shared.dylib"
    otool -L "$BUILD/source/libSPIRV-Tools-shared.dylib" | tail -n +2
    for exe in spirv-as spirv-dis spirv-val spirv-opt; do
        echo "===== otool -L tools/$exe"
        otool -L "$BUILD/tools/$exe" | tail -n +2
    done
} | norm > "$OUT/dylib-metadata.txt"
fi
grep -E '^ *[0-9]+/[0-9]+ Test|tests passed' "$BUILD/cmake-run.log" |
    sed -E 's/ +[0-9.]+ sec$//' > "$OUT/ctest.txt"
echo "baseline written to $OUT (build tree $BUILD)"
