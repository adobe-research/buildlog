#!/bin/sh
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The buildlog side of the bazel-flatc case study (README.md): configure and
# build case-studies/bazel-flatc/project once per configuration the Bazel
# baseline builds, normalize each build.ninja, compare it with
# bazel-<v>.jsonl, and record `buildlog why` for flatc's configuration.
#
#   run-buildlog.sh [native] [opt] [cross]      (default: all three)
#
# Writes buildlog-<v>.jsonl, compare-<v>.txt and (cross) why-flatc-*.txt
# here; build trees go under $WORK/bl-<v>.
#
# On Linux (linux/run.sh baselines; native and opt only) the files go to linux/
# or linux-clang/ (BASELINE_FAMILY=clang) here, and the preset is the ported one
# (arm64-linux, the image's compiler family).
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
SRC=${BUILDLOG_BAZEL_FLATC_SRC:-$HERE/../upstream}
SRC=$(cd "$SRC" && pwd)
WORK=${BAZEL_FLATC_WORK:-$REPO/build/case-bazel-flatc}
PROJECT=$REPO/case-studies/bazel-flatc/project
if [ "$(uname -s)" = Linux ]; then
  FAMILY=${BASELINE_FAMILY:-gcc}
  PRESET=$PROJECT/presets/$FAMILY.toml
  IN=$HERE/linux
  [ "$FAMILY" = gcc ] || IN=$HERE/linux-clang
  OUT_DIR=$IN
  P=arm64-linux
  DEFAULT="native opt"
else
  PRESET=$PROJECT/presets/apple-clang.toml
  CXX=$(xcrun --find clang++)
  IN=$HERE
  OUT_DIR=$HERE
  P=arm64-macos
  DEFAULT="native opt cross"
fi

roles() {
  case $1 in
    native) echo "--role $P.debug.none=target --role $P.release.none=exec" ;;
    opt) echo "--role $P.release.none=target,exec" ;;
    cross) echo "--role x86_64-macos.debug.none=target --role arm64-macos.release.none=exec" ;;
  esac
}

cd "$REPO"
for v in ${*:-$DEFAULT}; do
  echo "== $v"
  B=$WORK/bl-$v
  uv run --frozen buildlog configure --user "$PRESET" -D FLATBUFFERS_SRC="$SRC" --config "$v" \
    -C "$B" "$PROJECT" > /dev/null
  ninja -C "$B/souffle" -j2 > "$WORK/out/bl-$v-build.log"
  uv run --frozen python case-studies/halide/baseline/normalize.py normalize \
    --build-ninja "$B/souffle/build.ninja" --build "$B/souffle" --src "$SRC" \
    --scope "$HERE/buildlog-scope.toml" --variant "buildlog-$v" -o "$OUT_DIR/buildlog-$v.jsonl" > /dev/null
  # shellcheck disable=SC2046
  uv run --frozen python "$HERE/compare.py" "$IN/bazel-$v.jsonl" "$OUT_DIR/buildlog-$v.jsonl" \
    $(roles "$v") > "$OUT_DIR/compare-$v.txt"
done

# Why flatc is built for arm64-macos.release.none in the cross build, and
# where the exec policy (tools build release) comes from.
if [ -d "$WORK/bl-cross/souffle" ]; then
  uv run --frozen buildlog why -C "$WORK/bl-cross" --with-demand --flag "$CXX" \
    --output bin/arm64-macos.release.none/flatc "$PROJECT" \
    | sed -e "s#$REPO/##g" -e "s#$HOME#\$HOME#g" > "$HERE/why-flatc-cross.txt"
  uv run --frozen buildlog why -C "$WORK/bl-cross" --structural \
    'exec_config(["x86_64-macos", [["native.opt", "debug"], [["native.sanitize", "none"], nil]]], ["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], nil]]])' \
    "$PROJECT" | sed -e "s#$REPO/##g" -e "s#$HOME#\$HOME#g" > "$HERE/why-exec-config-cross.txt"
fi
