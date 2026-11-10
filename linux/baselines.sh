#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The upstream builds the case studies are compared with, on Linux: each study's
# own baseline script, run in the container (linux/run.sh baselines FAMILY).
# BUILDLOG_LINUX_TOOLCHAINS (gcc or clang) is the compiler family the upstream
# build uses (CC/CXX); the files go to the study's baseline/linux (gcc) or
# baseline/linux-clang, and are copied to /out/baselines/<study>/ for the host.
#
#   linux/baselines.sh [STUDY...]   zlib zlib-ng ggml fribidi flatbuffers spirv-tools nlohmann-json nanobind
#                                   bazel-flatc (Bazel, then buildlog and the comparison), halide-apps (the two apps' CMake builds)
set -uo pipefail

FAMILY="${BUILDLOG_LINUX_TOOLCHAINS:-gcc}"
export BASELINE_FAMILY="$FAMILY" JOBS="${JOBS:-8}"
git config --global --add safe.directory '*'
P=case-studies
DIR=linux
[[ "$FAMILY" == clang ]] && DIR=linux-clang
OUT=/out/baselines
mkdir -p "$OUT"
WANT=" ${*:-zlib zlib-ng ggml fribidi flatbuffers spirv-tools nlohmann-json nanobind bazel-flatc halide-apps} "
status=0

# study NAME SCRIPT [ARGS]; [DIR=study dir] run it and copy its Linux files (without raw/) out.
study() {
    local name="$1" script="$2" t0=$SECONDS dir="${STUDY_DIR:-$P/$1}"
    [[ "$WANT" == *" $name "* ]] || return 0
    if bash -c "$script" > "$OUT/$name-$FAMILY.log" 2>&1; then
        printf 'PASS %-28s %4ss\n' "baseline.$name" $((SECONDS - t0))
    else
        printf 'FAIL %-28s %4ss  (%s)\n' "baseline.$name" $((SECONDS - t0)) "$(tail -1 "$OUT/$name-$FAMILY.log" | cut -c1-120)"
        status=1
    fi
    rm -rf "${OUT:?}/$name-$FAMILY" && mkdir -p "$OUT/$name-$FAMILY"
    rsync -a --exclude raw "$dir/baseline/$DIR/" "$OUT/$name-$FAMILY/"
}

# zlib's baseline is the GCC one only (its comparison tests allow for the
# Clang flag spelling).
if [[ "$FAMILY" == gcc ]]; then
    study zlib "bash $P/zlib/baseline/run-cmake.sh"
fi
study zlib-ng "bash $P/zlib-ng/baseline/run-cmake.sh"
study ggml "bash $P/ggml/baseline/run-cmake.sh"
study fribidi "bash $P/fribidi/baseline/run-meson.sh"
study flatbuffers "bash $P/flatbuffers/baseline/run-cmake.sh"
study spirv-tools "bash $P/spirv-tools/baseline/run-cmake.sh"
study nlohmann-json "bash $P/nlohmann-json/baseline/run-cmake.sh"
study nanobind "bash $P/nanobind/baseline/run-baseline.sh"
# stdgpu needs the CUDA image: IMAGE=buildlog-cuda:13.4 linux/run.sh baselines gcc stdgpu
study stdgpu "bash $P/stdgpu/baseline/run-cmake.sh"
study bazel-flatc "bash $P/bazel-flatc/baseline/run-bazel.sh && bash $P/bazel-flatc/baseline/run-buildlog.sh"
# The apps live in the Halide case study (their compiler, emsdk and NDK are the image's).
export HALIDE_PREFIX="${BUILDLOG_HALIDE_PREFIX:-}"
# The Halide checkout is not in the copy: bring the two apps' sources.
if [[ " $WANT " == *" halide-apps "* ]]; then
    mkdir -p $P/halide/upstream/apps
    rsync -a /src/$P/halide/upstream/apps/HelloWasm /src/$P/halide/upstream/apps/HelloAndroid $P/halide/upstream/apps/
fi
STUDY_DIR=$P/halide/apps study halide-apps "bash $P/halide/apps/baseline/run-cmake.sh wasm && bash $P/halide/apps/baseline/run-cmake.sh android"
exit $status
