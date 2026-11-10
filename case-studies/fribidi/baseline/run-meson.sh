#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The fribidi case study's ground truth: fribidi v1.0.17's own Meson build
# (Meson's default buildtype, debug, and --buildtype=release, both Ninja), its
# tests, and the artifacts the buildlog build is compared with; and a cross
# build for x86_64.
#
#   run-meson.sh
#
# A third build, cross-x86_64, cross-compiles the library and programs for
# x86_64-macos with the cross file x86_64-darwin.ini (release); the gen.tab
# programs, `native: true`, stay arm64.
#
# Environment overrides:
#   FRIBIDI_SRC  the fribidi checkout (default ../upstream, the submodule,
#                at v1.0.17 = b93119f5). It is only read: the builds are out of tree.
#
# Both builds use -Ddocs=false: the default (docs=true) fails at setup from a git
# checkout, which has no pregenerated man pages and needs c2man and help2man
# (see the README's ledger).
#
# On Linux (linux/run.sh baselines) the debug and release builds go to baseline/linux
# (the image's GCC) or baseline/linux-clang (BASELINE_FAMILY=clang, Clang 18), with ELF's
# readelf and nm -D in place of otool and nm -gU and md5sum for md5 -r; there is no
# cross build there.
#
# Raw build trees and logs go to baseline/raw/ (gitignored; meson-logs/testlog.txt
# records the whole environment, so it must never be committed). The committed
# files next to this script are normalized: the source and build directories are
# replaced by $SRC and $BUILD (the JSONL graphs also $REPO, $HOME, $HOMEBREW, $SDK).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "${FRIBIDI_SRC:-$HERE/../upstream}" && pwd)"
OS="$(uname -s)"
OUT="$HERE"
VARIANTS=(debug release cross-x86_64)
if [[ "$OS" == Linux ]]; then
    OUT="$HERE/linux"
    [[ "${BASELINE_FAMILY:-gcc}" == clang ]] && { OUT="$HERE/linux-clang"; export CC=clang CXX=clang++; }
    VARIANTS=(debug release)
    mkdir -p "$OUT"
    MD5=(md5sum)
else
    MD5=(md5 -r)
fi
RAW="$OUT/raw"
PIN=b93119f5fdc7ea47672cc304c1455ffa6dfe7536

if [[ "$(git -C "$SRC" rev-parse HEAD)" != "$PIN" ]]; then
    echo "run-meson.sh: $SRC is not at v1.0.17 ($PIN)" >&2
    exit 1
fi
if [[ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]]; then
    echo "run-meson.sh: $SRC has local changes" >&2
    exit 1
fi

mkdir -p "$RAW"
# Meson writes the source directory relative to the build directory.
norm() {
    local rel
    rel="$(python3 -c 'import os, sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$SRC" "$BUILD")"
    sed -E -e "s#$BUILD/${rel//./\\.}([/ \"]|\$)#\$SRC\1#g" \
        -e "s#${rel//./\\.}([/ \"]|\$)#\$SRC\1#g" -e "s#$BUILD#\$BUILD#g" -e "s#$SRC#\$SRC#g"
}
{
    echo "# Environment"
    uname -sm
    if [[ "$OS" == Darwin ]]; then sw_vers -productVersion; else . /etc/os-release && echo "$PRETTY_NAME"; fi
    echo "meson $(meson --version)"
    echo "ninja $(ninja --version)"
    ${CC:-cc} --version | head -1
    if command -v ccache > /dev/null; then ccache --version | head -1; fi
    echo "fribidi $(git -C "$SRC" describe --tags) $(git -C "$SRC" rev-parse HEAD)"
} > "$OUT/environment.txt"

for variant in "${VARIANTS[@]}"; do
    BUILD="$RAW/meson-$variant"
    rm -rf "$BUILD"
    args=(-Ddocs=false)
    [[ $variant != debug ]] && args+=(--buildtype=release)
    [[ $variant == cross-x86_64 ]] && args+=(--cross-file "$HERE/x86_64-darwin.ini")
    meson setup "${args[@]}" "$BUILD" "$SRC" > "$RAW/setup-$variant.log" 2>&1
    meson compile -C "$BUILD" -j 2 > "$RAW/compile-$variant.log" 2>&1
    # A cross build's tests that run an x86_64 program directly are skipped
    # (exit 77: no exe_wrapper); meson test then still exits 0.
    meson test -C "$BUILD" --num-processes 2 > "$RAW/test-$variant.log" 2>&1

    ninja -C "$BUILD" -t commands | norm > "$OUT/commands-$variant.txt"
    python3 "$HERE/normalize_meson.py" --build "$BUILD" --src "$SRC" --variant "$variant" \
        -o "$OUT/meson-$variant.jsonl" 2> /dev/null
    # The configure checks, as Meson reports them at setup.
    grep -E '^(Checking for|Has header|Checking whether)' "$RAW/setup-$variant.log" |
        sed -e 's/ (cached)//' -e 's/\x1b\[[0-9;]*m//g' > "$OUT/checks-$variant.txt"
    # The options in effect: the project's, and the built-ins that change the graph.
    meson introspect --buildoptions "$BUILD" | python3 -c '
import json, sys
keep = {"buildtype", "debug", "optimization", "b_ndebug", "default_library", "warning_level",
        "werror", "b_lundef", "b_pie", "b_staticpic", "b_asneeded", "b_sanitize", "c_std",
        "prefix", "libdir", "includedir", "bindir"}
for o in sorted(json.load(sys.stdin), key=lambda o: (o["section"], o["name"])):
    if o["section"] == "user" or o["name"] in keep:
        print("{:8} {:16} {}".format(o["section"], o["name"], json.dumps(o["value"])))
' > "$OUT/options-$variant.txt"
    grep -E '^ *[0-9]+/[0-9]+ ' "$RAW/test-$variant.log" |
        sed -E -e 's/ +[0-9.]+s( .*)?$/\1/' -e 's/\x1b\[[0-9;]*m//g' -e 's/^ *[0-9]+\/[0-9]+ //' |
        sort > "$OUT/meson-test-$variant.txt"
done

BUILD="$RAW/meson-release"
mkdir -p "$OUT/release"
meson introspect --targets "$BUILD" | python3 -m json.tool | norm > "$OUT/release/intro-targets.json"
# The test definitions without their environment (Meson adds none of the user's
# here, but keep the committed file independent of that).
meson introspect --tests "$BUILD" | python3 -c '
import json, sys
tests = json.load(sys.stdin)
for t in tests:
    t.pop("env", None)
json.dump(tests, sys.stdout, indent=2, sort_keys=True)
' | norm > "$OUT/release/intro-tests.json"
norm < "$BUILD/compile_commands.json" > "$OUT/release/compile_commands.json"
norm < "$BUILD/config.h" > "$OUT/release/config.h"
norm < "$BUILD/lib/fribidi-config.h" > "$OUT/release/fribidi-config.h"
diff "$BUILD/config.h" "$RAW/meson-debug/config.h" > "$OUT/release/config.h.debug-diff" || true
cp "$BUILD/gen.tab/fribidi-unicode-version.h" "$OUT/release/"
(cd "$BUILD/gen.tab" && "${MD5[@]}" fribidi-unicode-version.h ./*.tab.i | sed 's# \./# #' | sort -k2) \
    > "$OUT/release/tables.md5"
(cd "$RAW/meson-debug/gen.tab" && "${MD5[@]}" fribidi-unicode-version.h ./*.tab.i | sed 's# \./# #' | sort -k2) |
    diff "$OUT/release/tables.md5" - > /dev/null ||
    { echo "run-meson.sh: debug and release tables differ" >&2; exit 1; }
if [[ "$OS" == Linux ]]; then
    nm -D --defined-only "$BUILD/lib/libfribidi.so.0" > "$OUT/release/exported-symbols.txt"
    {
        echo "===== files"
        (cd "$BUILD" && find lib bin test gen.tab -maxdepth 2 \( -type f -perm -u+x -o -type l \) \
            ! -path '*.p/*' | sort)
        echo "===== links"
        for l in libfribidi.so libfribidi.so.0; do echo "lib/$l -> $(readlink "$BUILD/lib/$l")"; done
        echo "===== readelf -d lib/libfribidi.so.0"
        readelf -d "$BUILD/lib/libfribidi.so.0" | grep -E 'NEEDED|SONAME|RUNPATH|RPATH' | awk '{$1=""; print substr($0,2)}'
        for exe in bin/fribidi test/unicode-conformance/BidiTest gen.tab/gen-bidi-type-tab; do
            echo "===== readelf -d $exe"
            readelf -d "$BUILD/$exe" | grep -E 'NEEDED|RUNPATH|RPATH' | awk '{$1=""; print substr($0,2)}'
        done
    } | norm > "$OUT/release/elf-metadata.txt"
    echo "baseline written to $OUT"
    exit 0
fi
nm -gU "$BUILD/lib/libfribidi.0.dylib" > "$OUT/release/exported-symbols.txt"
{
    echo "===== files"
    (cd "$BUILD" && find lib bin test gen.tab -maxdepth 2 \( -type f -perm -u+x -o -type l \) \
        ! -path '*.p/*' | sort)
    echo "===== links"
    echo "lib/libfribidi.dylib -> $(readlink "$BUILD/lib/libfribidi.dylib")"
    echo "===== otool -L lib/libfribidi.0.dylib"
    otool -L "$BUILD/lib/libfribidi.0.dylib" | tail -n +2
    echo "===== LC_ID_DYLIB"
    otool -l "$BUILD/lib/libfribidi.0.dylib" | grep -A5 LC_ID_DYLIB | grep -E 'name|version'
    for exe in bin/fribidi test/unicode-conformance/BidiTest gen.tab/gen-bidi-type-tab; do
        echo "===== otool -L $exe"
        otool -L "$BUILD/$exe" | tail -n +2
        echo "===== rpaths $exe"
        otool -l "$BUILD/$exe" | grep -A2 LC_RPATH | grep path || true
    done
} | norm > "$OUT/release/dylib-metadata.txt"
echo "baseline written to $OUT"
