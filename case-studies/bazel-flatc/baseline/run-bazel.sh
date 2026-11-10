#!/bin/sh
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The Bazel baseline of the bazel-flatc case study (README.md): flatbuffers'
# own Bazel rules (build_defs.bzl's flatbuffer_cc_library, //:flatc) built
# through a consumer module (consumer/), in three configurations.
#
#   run-bazel.sh [native] [opt] [cross]      (default: all three)
#
#   native  bazel build //:sample_binary                    (-c fastbuild)
#   opt     bazel build -c opt //:sample_binary
#   cross   bazel build --platforms=@build_bazel_apple_support//platforms:darwin_x86_64
#                       --experimental_platform_in_output_dir //:sample_binary
#
# Per variant it writes, into this directory: bazel-<v>.jsonl (the normalized
# aquery graph), bazel-<v>-summary.txt, bazel-<v>-evidence.txt (cquery with
# transitions, config diff, toolchain resolution, file types, run output).
# Large files (the raw aquery, logs, the output base) stay under $WORK.
#
# On Linux (linux/run.sh baselines) only native and opt exist (the cross variant
# targets macOS's x86_64 through apple_support, which Linux has no use for), the
# files go to linux/ (the image's GCC) or linux-clang/ (BASELINE_FAMILY=clang)
# next to this script, and rules_cc auto-configures the image's compiler.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
SRC=${BUILDLOG_BAZEL_FLATC_SRC:-$HERE/../upstream}
SRC=$(cd "$SRC" && pwd)
WORK=${BAZEL_FLATC_WORK:-$REPO/build/case-bazel-flatc}
CONSUMER=$WORK/consumer
OUT=$WORK/out
BAZEL="bazel --output_user_root=$WORK/bazel-root"
OS=$(uname -s)
OUT_DIR=$HERE
if [ "$OS" = Linux ]; then
  OUT_DIR=$HERE/linux
  if [ "${BASELINE_FAMILY:-gcc}" = clang ]; then
    OUT_DIR=$HERE/linux-clang
    export CC=clang CXX=clang++
  fi
  mkdir -p "$OUT_DIR"
fi
sha256() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1"; else sha256sum "$1"; fi | cut -d' ' -f1; }

mkdir -p "$CONSUMER" "$OUT"
if [ "$OS" = Linux ]; then
  # apple_support is the macOS toolchain's module: nothing on Linux uses it.
  sed "s#@FLATBUFFERS_SRC@#$SRC#" "$HERE/consumer/MODULE.bazel.in" | grep -v apple_support > "$CONSUMER/MODULE.bazel"
else
  sed "s#@FLATBUFFERS_SRC@#$SRC#" "$HERE/consumer/MODULE.bazel.in" > "$CONSUMER/MODULE.bazel"
fi
cp "$HERE/consumer/BUILD.bazel" "$HERE/consumer/.bazelversion" "$CONSUMER/"
cp "$SRC/samples/monster.fbs" "$SRC/samples/sample_binary.cpp" "$CONSUMER/"

flags() {
  case $1 in
    native) echo "" ;;
    opt) echo "-c opt" ;;
    cross) echo "--platforms=@build_bazel_apple_support//platforms:darwin_x86_64 --experimental_platform_in_output_dir" ;;
    *) echo "unknown variant $1" >&2; exit 2 ;;
  esac
}

variant() {
  v=$1
  f=$(flags "$v")
  ev="$OUT_DIR/bazel-$v-evidence.txt"
  cd "$CONSUMER"
  # shellcheck disable=SC2086
  $BAZEL build --jobs=2 $f //:sample_binary > "$OUT/$v-build.log" 2>&1
  # shellcheck disable=SC2086
  $BAZEL aquery $f --output=jsonproto 'deps(//:sample_binary)' > "$OUT/$v-aquery.json" 2> "$OUT/$v-aquery.err"
  (cd "$REPO" && uv run --frozen python "$HERE/normalize_aquery.py" normalize "$OUT/$v-aquery.json" \
    --variant "bazel-$v" --map 'external/flatbuffers+=$SRC' --main '$SRC/samples' \
    --exclude-label '^@apple_support//=bazel: apple_support toolchain wrappers (built for exec)' \
    -o "$OUT_DIR/bazel-$v.jsonl") > "$OUT_DIR/bazel-$v-summary.txt"
  bin=$($BAZEL cquery $f --output=files //:sample_binary 2>/dev/null)
  flatc=$($BAZEL cquery $f --output=files 'filter("flatc$", deps(//:sample_binary))' 2>/dev/null \
    | grep '/flatc$')
  {
    echo "# bazel $(cat .bazelversion), flags: ${f:-(none)}"
    echo
    echo "## cquery 'deps(//:sample_binary)': the slice's targets and their configurations"
    # shellcheck disable=SC2086
    $BAZEL cquery $f 'deps(//:sample_binary)' 2>/dev/null \
      | grep -E '^(//:|@com_github_google_flatbuffers//(:flatc|:flatbuffers|:runtime_cc|src:flatbuffers|src:flatc) )' | sort
    echo
    echo "## cquery --transitions=lite 'deps(//:monster_fbs_srcs, 1)': the genrule's tools edge"
    # shellcheck disable=SC2086
    $BAZEL cquery $f --transitions=lite 'deps(//:monster_fbs_srcs, 1)' 2>/dev/null \
      | sed -n '/-> \/\/:monster_fbs_srcs/,/^[^ ]/p' | sed '$d'
    echo
    tcfg=$($BAZEL cquery $f //:sample_binary 2>/dev/null | sed 's/.*(\(.*\))/\1/')
    ecfg=$($BAZEL cquery $f 'filter("flatc$", deps(//:sample_binary))' 2>/dev/null \
      | grep '//:flatc ' | sed 's/.*(\(.*\))/\1/')
    echo "## bazel config $tcfg $ecfg: what the exec transition changed"
    $BAZEL config "$tcfg" "$ecfg" 2>/dev/null
    echo
    echo "## toolchain resolution (--toolchain_resolution_debug=@bazel_tools//tools/cpp:toolchain_type)"
    # shellcheck disable=SC2086
    $BAZEL cquery $f --toolchain_resolution_debug='@bazel_tools//tools/cpp:toolchain_type' \
      '//:monster_fbs_srcs + @com_github_google_flatbuffers//:flatc' 2>&1 >/dev/null \
      | grep -E 'ToolchainResolution: (Target platform .*Selected execution platform|  Toolchain .* is compatible|.*Rejected toolchain)' \
      | sed -e 's/^INFO: //' -e 's/^ *//' | sort -u
    echo
    echo "## artifacts"
    file -b "$bin" | sed 's/^/sample_binary: /'
    file -b "$flatc" | sed 's/^/flatc: /'
    echo "sample_binary: ${bin#bazel-out/}" | sed 's#/.*##'
    echo "flatc: ${flatc#bazel-out/}" | sed 's#/.*##'
    hdr=$(dirname "$bin")/monster_generated.h
    echo "monster_generated.h sha256: $(sha256 "$hdr")"
    echo
    echo "## run"
    case $v in cross) arch -x86_64 "./$bin" ;; *) "./$bin" ;; esac
    if [ "$v" = cross ]; then
      echo
      echo "## without --experimental_platform_in_output_dir: the configurations' output dirs"
      $BAZEL aquery --platforms=@build_bazel_apple_support//platforms:darwin_x86_64 \
        --output=jsonproto '//:sample_binary' 2>/dev/null \
        | python3 -c 'import json,sys; [print(c["mnemonic"], "isTool" in c) for c in json.load(sys.stdin)["configuration"]]'
      echo
      echo "## the annotation experiment: flatc in srcs, not tools (//:monster_misannotated)"
      # shellcheck disable=SC2086
      $BAZEL build --jobs=2 $f //:monster_misannotated > "$OUT/$v-misannotated.log" 2>&1 \
        && echo "bazel build //:monster_misannotated: succeeded"
      # shellcheck disable=SC2086
      $BAZEL cquery $f --transitions=lite 'deps(//:monster_misannotated, 1)' 2>/dev/null \
        | grep 'srcs#@com_github_google_flatbuffers//:flatc'
      # shellcheck disable=SC2086
      mis=$($BAZEL aquery $f 'mnemonic(Genrule, //:monster_misannotated)' 2>/dev/null \
        | grep -o 'bazel-out/[^ ]*/flatc' | head -1)
      echo "the genrule runs $mis: $(file -b "$mis")"
    fi
  } > "$ev"
  cd "$HERE"
}

if [ "$OS" = Linux ]; then default="native opt"; else default="native opt cross"; fi
variants=${*:-$default}
for v in $variants; do
  echo "== $v"
  variant "$v"
done
