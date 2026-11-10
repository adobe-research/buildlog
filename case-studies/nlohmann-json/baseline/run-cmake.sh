#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The nlohmann/json case study's ground truth: v3.12.0's own CMake configure
# and install (Ninja), twice: with every option at its default, and with
# JSON_Diagnostics=ON. Then two downstreams of each install: a CMake
# find_package(nlohmann_json 3.2 CONFIG REQUIRED) consumer and a pkg-config
# one, both compiling ../downstream/main.cpp.
#
#   run-cmake.sh
#
# Environment overrides:
#   JSON_SRC   the nlohmann/json checkout (default ../upstream, the submodule,
#              at v3.12.0 = 55f93686). It is only read: the build is out of tree.
#   RAW        where the build and install trees go (default
#              <repo>/build/case-nlohmann/baseline, untracked).
#
# On Linux (linux/run.sh baselines) the files go to baseline/linux (the image's GCC) or
# baseline/linux-clang (BASELINE_FAMILY=clang, Clang 18), with stat -c and sha256sum.
#
# The committed files next to this script are normalized: the source, build
# and install directories are $SRC, $BUILD and $PREFIX.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWN="$(cd "$HERE/../downstream" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
SRC="$(cd "${JSON_SRC:-$HERE/../upstream}" && pwd)"
RAW="${RAW:-$REPO/build/case-nlohmann/baseline}"
PIN=55f93686c01528224f448c19128836e7df245f72
OS="$(uname -s)"
OUTROOT="$HERE"
SIZE=(stat -f %z)
SHA=(shasum -a 256)
if [[ "$OS" == Linux ]]; then
    OUTROOT="$HERE/linux"
    [[ "${BASELINE_FAMILY:-gcc}" == clang ]] && { OUTROOT="$HERE/linux-clang"; export CC=clang CXX=clang++; }
    SIZE=(stat -c %s)
    SHA=(sha256sum)
    mkdir -p "$OUTROOT"
fi

if [[ "$(git -C "$SRC" rev-parse HEAD)" != "$PIN" ]]; then
    echo "run-cmake.sh: $SRC is not at v3.12.0 ($PIN)" >&2
    exit 1
fi
if [[ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]]; then
    echo "run-cmake.sh: $SRC has local changes" >&2
    exit 1
fi
mkdir -p "$RAW"
RAW="$(cd "$RAW" && pwd)"

{
    echo "# Environment"
    uname -sm
    if [[ "$OS" == Darwin ]]; then sw_vers -productVersion; else . /etc/os-release && echo "$PRETTY_NAME"; fi
    cmake --version | head -1
    echo "ninja $(ninja --version)"
    echo "pkg-config $(pkg-config --version)"
    ${CXX:-c++} --version | head -1
    echo "nlohmann/json $(git -C "$SRC" describe --tags) $(git -C "$SRC" rev-parse HEAD)"
} > "$OUTROOT/environment.txt"

# One variant: $1 = name (default | diagnostics), the rest = cmake -D options.
variant() {
    local name="$1"
    shift
    local build="$RAW/cmake-build-$name" prefix="$RAW/cmake-install-$name"
    local out="$OUTROOT/$name"
    rm -rf "$build" "$prefix" "$RAW/consumer-$name" "$out"
    mkdir -p "$out"
    norm() { sed -e "s#$build#\$BUILD#g" -e "s#$prefix#\$PREFIX#g" -e "s#$SRC#\$SRC#g"; }
    {
        # JSON_BuildTests defaults to ON for a main project; the unit tests
        # are out of the study's scope (and download test data).
        cmake -S "$SRC" -B "$build" -G Ninja -DCMAKE_INSTALL_PREFIX="$prefix" \
            -DJSON_BuildTests=OFF "$@"
        cmake --build "$build"
        cmake --install "$build"
    } > "$RAW/cmake-$name.log" 2>&1
    grep -E '^(JSON_[A-Za-z_]+|CMAKE_INSTALL_(PREFIX|INCLUDEDIR|DATADIR|LIBDIR))(:[A-Z]+)?=' \
        "$build/CMakeCache.txt" | norm | sort > "$out/cache.txt"
    # The installed tree: every file, its size and SHA-256.
    (cd "$prefix" && find . -type f | LC_ALL=C sort | while read -r f; do
        printf '%s %s %s\n' "$f" "$("${SIZE[@]}" "$f")" "$("${SHA[@]}" "$f" | cut -d' ' -f1)"
    done) > "$out/installed-tree.txt"
    norm < "$prefix/share/pkgconfig/nlohmann_json.pc" > "$out/nlohmann_json.pc"
    for f in nlohmann_jsonConfig.cmake nlohmann_jsonConfigVersion.cmake nlohmann_jsonTargets.cmake; do
        norm < "$prefix/share/cmake/nlohmann_json/$f" > "$out/$f"
    done

    # Downstream 1: CMake find_package against the install.
    local cb="$RAW/consumer-$name/cmake"
    {
        cmake -S "$DOWN/cmake" -B "$cb" -G Ninja -DCMAKE_PREFIX_PATH="$prefix" \
            -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
        cmake --build "$cb"
    } > "$RAW/consumer-$name-cmake.log" 2>&1
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[0]["command"])' \
        "$cb/compile_commands.json" | sed -e "s#$cb#\$CONSUMER#g" -e "s#$DOWN#\$DOWNSTREAM#g" \
        | norm > "$out/consumer-cmake-command.txt"
    "$cb/app" > "$out/consumer-cmake-output.txt"

    # Downstream 2: pkg-config against the install.
    local pb="$RAW/consumer-$name/pkg-config"
    mkdir -p "$pb"
    local flags
    flags="$(PKG_CONFIG_PATH="$prefix/share/pkgconfig" pkg-config --cflags nlohmann_json)"
    echo "$flags" | norm > "$out/consumer-pkg-config-cflags.txt"
    # shellcheck disable=SC2086
    ${CXX:-c++} -std=c++17 $flags "$DOWN/main.cpp" -o "$pb/app"
    "$pb/app" > "$out/consumer-pkg-config-output.txt"
}

variant default
variant diagnostics -DJSON_Diagnostics=ON
echo "baseline written to $OUTROOT (raw trees in $RAW)"
