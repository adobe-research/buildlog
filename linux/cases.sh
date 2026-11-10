#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The case studies on Linux: configure, build and test each with the
# container's toolchain (BUILDLOG_LINUX_TOOLCHAINS, gcc or clang), using
# the ported presets (linux/port.py). Runs inside the container from the
# worktree copy (linux/run.sh cases FAMILY); one summary line per step.
# Only the native configuration is selected where a case study also declares
# macOS cross configurations (x86_64, universal2).
#
#   linux/cases.sh [STUDY...]   stdgpu (gcc, CUDA image only) zlib nlohmann zlib-ng ggml fribidi flatbuffers spirv-tools nanobind
#                               qt (Qt 6.11.3 via imports), bazel-flatc (native and opt, with the Bazel comparison),
#                               hellowasm and helloandroid (emsdk; the emulated x86_64 NDK; the CMake comparison;
#                               they need the baselines: linux/run.sh baselines)
#                               halide (GCC only: LLVM 22 as in docker scenario 7b; about 3 minutes cold,
#                               in the default list only for gcc)
set -uo pipefail

B=/work/build
mkdir -p "$B"
FAMILY="${BUILDLOG_LINUX_TOOLCHAINS:-gcc}"
LOGS="/out/cases-$FAMILY"
rm -rf "$LOGS" && mkdir -p "$LOGS"
status=0

# step NAME CMD...: runs CMD, logs it to $LOGS/NAME.log, prints PASS/FAIL.
step() {
    local name="$1"
    shift
    local t0=$SECONDS
    if "$@" >"$LOGS/$name.log" 2>&1; then
        printf 'PASS %-28s %4ss\n' "$name" $((SECONDS - t0))
    else
        printf 'FAIL %-28s %4ss  (%s)\n' "$name" $((SECONDS - t0)) "$(tail -1 "$LOGS/$name.log" | cut -c1-120)"
        status=1
    fi
}

# expect_error TEXT CMD...: CMD must fail and say TEXT.
expect_error() {
    local text="$1" out
    shift
    out=$("$@" 2>&1) && { echo "$out"; return 1; }
    echo "$out"
    [[ "$out" == *"$text"* ]]
}

bl() { uv run --frozen buildlog "$@"; }
P=case-studies
DEFAULT="zlib nlohmann zlib-ng ggml fribidi flatbuffers spirv-tools nanobind qt bazel-flatc hellowasm helloandroid"
[[ "$FAMILY" == gcc ]] && DEFAULT="$DEFAULT halide"  # clang: name it explicitly
WANT=" ${*:-$DEFAULT} "
want() { [[ "$WANT" == *" $1 "* ]]; }

if want zlib; then
# zlib: both configurations, the tests, then the Linux CMake baseline comparison
step zlib.configure bl configure --user $P/zlib/project/presets/$FAMILY-arm64.toml -C $B/zlib $P/zlib/project
step zlib.build ninja -C $B/zlib/souffle
step zlib.test bl test -C $B/zlib
fi

if want nlohmann; then
# nlohmann-json: the export files, a staged install, the downstream against it
NP=$P/nlohmann-json/project
ND=$P/nlohmann-json/downstream/buildlog
step nlohmann.configure bl configure --user $NP/presets/$FAMILY-arm64.toml \
    --user $NP/presets/install-prefix.toml -D INSTALL_PREFIX=/work/opt/nlohmann -C $B/nl $NP
step nlohmann.build ninja -C $B/nl/souffle
step nlohmann.install env DESTDIR=/ ninja -C $B/nl/souffle install
step nlohmann.downstream.configure bl configure --user $ND/presets/$FAMILY-arm64.toml \
    --user $ND/presets/import-cps.toml -D NLOHMANN_PREFIX=/work/opt/nlohmann -C $B/down $ND
step nlohmann.downstream.build ninja -C $B/down/souffle
step nlohmann.downstream.test bl test -C $B/down
fi

if want zlib-ng; then
# zlib-ng: the native release configuration
step zlib-ng.configure bl configure --user $P/zlib-ng/project/presets/$FAMILY.toml --config release \
    -C $B/zlib-ng $P/zlib-ng/project
step zlib-ng.build ninja -C $B/zlib-ng/souffle
step zlib-ng.test bl test -C $B/zlib-ng $P/zlib-ng/project
fi

if want ggml; then
# ggml: linked (the build machine's CPU stated as upstream's CMake reads it),
# then the dlopen'd per-ISA variants.
GP=$P/ggml/project
CCBIN=cc; [ "$FAMILY" = clang ] && CCBIN=clang
python3 $P/ggml/native_cpu.py $CCBIN > $B/native-cpu.toml
step ggml.configure bl configure -C $B/ggml --user $GP/presets/$FAMILY.toml --user $B/native-cpu.toml \
    -D GGML_BUILD_COMMIT=353b63b4 $GP
step ggml.build ninja -C $B/ggml/souffle
step ggml.test bl test -C $B/ggml
if [ "$(uname -m)" = aarch64 ] && [ "$FAMILY" = gcc ]; then
    # GCC 13 has no +sme: the armv9.2 levels are an error (upstream's CMake fails to compile them).
    step ggml.variants.sme-error expect_error "gcc: sme (armv9.2_1)" bl configure -C $B/ggml-variants \
        --config release --user $GP/presets/gcc.toml --user $GP/presets/variants.toml \
        -D GGML_BUILD_COMMIT=353b63b4 $GP
else
step ggml.variants.configure bl configure -C $B/ggml-variants --config release \
    --user $GP/presets/$FAMILY.toml --user $GP/presets/variants.toml -D GGML_BUILD_COMMIT=353b63b4 $GP
step ggml.variants.build ninja -C $B/ggml-variants/souffle
step ggml.variants.test bl test -C $B/ggml-variants
fi
fi

# stdgpu: needs the CUDA image (IMAGE=buildlog-cuda:13.4), so not in the default
# list; compile and link only, the programs need a GPU.
if want stdgpu; then
SP=$P/stdgpu
step stdgpu.configure bl configure --user $SP/project/presets/gcc-ampere.toml -C $B/stdgpu $SP/project
step stdgpu.build ninja -C $B/stdgpu/souffle
step stdgpu.compare python3 $SP/compare.py $SP/baseline/linux/commands.txt $B/stdgpu --src "$PWD/$SP/upstream"
fi

if want fribidi; then
# fribidi
step fribidi.configure bl configure --user $P/fribidi/project/presets/$FAMILY-arm64.toml \
    --config release -C $B/fribidi $P/fribidi/project
step fribidi.build ninja -C $B/fribidi/souffle
step fribidi.test bl test -C $B/fribidi
fi

if want flatbuffers; then
    # flatbuffers: the native release configuration (the x86_64 and universal2
    # ones are macOS cross builds); flatc is built for the host and runs the generators.
    FP=$P/flatbuffers/project
    step flatbuffers.configure bl configure --user $FP/presets/$FAMILY.toml --config release -C $B/flatbuffers $FP
    step flatbuffers.build ninja -C $B/flatbuffers/souffle
    step flatbuffers.test bl test -C $B/flatbuffers
fi

if want spirv-tools; then
    # SPIRV-Tools: SPIRV-Headers and googletest as installed packages (the
    # image's /opt/spirv-headers and /opt/googletest), the distribution's python3.
    SP=$P/spirv-tools/project/presets
    step spirv-tools.configure bl configure --user $SP/$FAMILY-arm64.toml --user $SP/linux-python.toml \
        --user $SP/spirv-headers-prefix.toml --user $SP/prefix-googletest.toml \
        -D SPIRV_HEADERS_PREFIX=$BUILDLOG_SPIRV_HEADERS_PREFIX -D GOOGLETEST_PREFIX=/opt/googletest \
        -C $B/spirv-tools $P/spirv-tools/project
    step spirv-tools.build ninja -C $B/spirv-tools/souffle
    step spirv-tools.test bl test -C $B/spirv-tools
fi

if want nanobind; then
    # nanobind_example for the two uv-managed CPythons in the image (3.13 stable
    # ABI, 3.11 full ABI; the venvs hold nanobind): configure, build, and the
    # example's tests on the staged package trees.
    NP=$P/nanobind/project
    step nanobind.configure bl configure --package-path $P/nanobind --user $NP/presets/$FAMILY-arm64.toml --user $NP/presets/interpreters.toml \
        -C $B/nanobind $NP
    step nanobind.build ninja -C $B/nanobind/souffle
    for t in 313 311; do
        step nanobind.test-$t env PYTHONPATH=$B/nanobind/souffle/python/arm64-linux.release.none.cp$t \
            $BUILDLOG_NANOBIND_VENVS/venv$t/bin/python -m pytest -q -p no:cacheprovider -o addopts= \
            $P/nanobind/upstream/tests
    done
fi

if want halide; then
    # libHalide (GCC 13, LLVM 22 from apt.llvm.org; the 7b ground truth): configure, build
    # everything, the 32-test sample, then the graph comparison with the CMake baseline.
    # Halide's source is the submodule (not in the worktree copy), taken from the mount.
    H=$P/halide
    mkdir -p $H/upstream && rsync -a --exclude .git /src/$H/upstream/ $H/upstream/
    step halide.configure bl configure $H/libhalide -C $B/halide --package-path $H/packages --update-lock \
        --user $H/libhalide/presets/linux-$FAMILY-arm64.toml
    step halide.build ninja -C $B/halide/souffle -j8 ${HALIDE_NINJA_ARGS:-}
    step halide.test bl test -C $B/halide -j 4 ${HALIDE_TEST_R:+-R "$HALIDE_TEST_R"} ${HALIDE_TEST_ARGS:-}
    TOTALS='^total *870 *1322 *442 *1$'; [ "$FAMILY" = gcc ] || TOTALS='^total'  # clang: the totals are only reported
    step halide.compare bash -c "uv run --frozen python $H/phase1/compare_libhalide.py $B/halide \
        --variant linux-$FAMILY-release --phase 1 --phase 2 --phase 3 --phase 4 --phase 5 --phase 6 --details 0 \
        | tee /out/halide-compare-$FAMILY.txt | grep -q '$TOTALS'"
fi

if want qt; then
    # Qt 6.11.3 (aqt's linux_gcc_arm64, as the macOS study's 6.11.3): the rule packages installed
    # into an overlay of Qt's prefix as Qt would ship them, imports of Qt's own CMake files, moc and
    # rcc run as host tools. The consumer in CMake is built and run for the comparison.
    Q=$P/qt-moc
    QP=$B/qt-prefix
    mkdir -p $QP/share/buildlog
    for d in lib libexec plugins include mkspecs bin; do ln -sfn $BUILDLOG_QT_PREFIX/$d $QP/$d; done
    cp -R $Q/share/buildlog/. $QP/share/buildlog/
    export QT_QPA_PLATFORM=offscreen
    step qt.configure bl configure --package-path $QP -D QT_PREFIX=$QP --user $Q/project/presets/$FAMILY.toml \
        --config debug --config release -C $B/qt $Q/project
    step qt.build ninja -C $B/qt/souffle
    step qt.test bl test -C $B/qt
    step qt.cmake.configure cmake -G Ninja -S $Q/cmake -B $B/qt-cmake -DCMAKE_PREFIX_PATH=$BUILDLOG_QT_PREFIX \
        -DCMAKE_BUILD_TYPE=Release
    step qt.cmake.build cmake --build $B/qt-cmake
    step qt.cmake.test ctest --test-dir $B/qt-cmake --output-on-failure
fi

if want bazel-flatc; then
    # flatc built for the exec configuration and run by an action, in the native
    # (debug target, release exec) and opt variants (no cross variant on Linux). Compared with
    # the checked-in Bazel baseline (linux/run.sh baselines).
    BZ=$P/bazel-flatc
    BD=$BZ/baseline/linux; [ "$FAMILY" = clang ] && BD=$BZ/baseline/linux-clang
    for v in native opt; do
        step bazel-flatc.$v.configure bl configure --user $BZ/project/presets/$FAMILY.toml \
            -D FLATBUFFERS_SRC=$PWD/$BZ/upstream --config $v -C $B/bzl-$v $BZ/project
        step bazel-flatc.$v.build ninja -C $B/bzl-$v/souffle
        step bazel-flatc.$v.compare bash -c "uv run --frozen python $P/halide/baseline/normalize.py normalize \
            --build-ninja $B/bzl-$v/souffle/build.ninja --build $B/bzl-$v/souffle --src $PWD/$BZ/upstream \
            --scope $BZ/baseline/buildlog-scope.toml --variant buildlog-$v -o $B/bzl-$v.jsonl >/dev/null \
            && uv run --frozen python $BZ/baseline/compare.py $BD/bazel-$v.jsonl $B/bzl-$v.jsonl \
            \$([ $v = native ] && echo '--role arm64-linux.debug.none=target --role arm64-linux.release.none=exec' \
                || echo '--role arm64-linux.release.none=target,exec') | tee /out/bazel-flatc-$v-compare-$FAMILY.txt \
            | grep -q 'tool built in: Bazel exec, buildlog'"
    done
    step bazel-flatc.header sha256sum -c <(echo "b735c911b19b778f954531add4978e49894627793c465da8d4b84f84de2bb8ac  $B/bzl-native/souffle/gen/arm64-linux.debug.none/monster_fbs/monster_generated.h")
fi

if want hellowasm || want helloandroid; then
    # The two apps of Halide's apps/, with the specs of the macOS study. The Halide checkout is
    # not in the copy: only the two apps' sources are needed (the CMake comparison reads them).
    A=$P/halide/apps
    AB=$A/baseline/linux; [ "$FAMILY" = clang ] && AB=$A/baseline/linux-clang
    mkdir -p $P/halide/upstream/apps
    rsync -a /src/$P/halide/upstream/apps/HelloWasm /src/$P/halide/upstream/apps/HelloAndroid $P/halide/upstream/apps/
    appbl() { bl configure $A/$1 -C $B/$1 --package-path $P/halide/packages -D HALIDE_PREFIX=$BUILDLOG_HALIDE_PREFIX \
        --user $A/$1/presets/linux.toml; }
fi

if want hellowasm; then
    # Emscripten 3.1.73 (emsdk in the image): generators run on the host, the app is compiled to wasm
    # by flags derived from the rules (the spec has no flag combinations).
    step hellowasm.configure appbl hellowasm
    step hellowasm.build ninja -C $B/hellowasm/souffle
    step hellowasm.compare bash -c "python3 $A/baseline/commands.py compare --variants $A/baseline/hellowasm-variants.json \
        --root PREFIX=$BUILDLOG_HALIDE_PREFIX --root EMSDK=$EMSDK --root SRC=$PWD/$P/halide/upstream/apps/HelloWasm \
        --root BUILD=/nonexistent $AB/cmake-hellowasm.json $B/hellowasm/souffle | tee /out/hellowasm-compare-$FAMILY.txt \
        | grep -q '^generate  baseline  15  buildlog  15  same key  15  identical  15 '"
fi

if want helloandroid; then
    # The NDK (r30-beta2) ships x86_64 Linux tools only: they run in the arm64 container through
    # binfmt (Docker Desktop's x86_64 emulation) with amd64 libc/libstdc++/zlib installed.
    step helloandroid.configure appbl helloandroid
    step helloandroid.build ninja -C $B/helloandroid/souffle
    step helloandroid.abis bash -c "file -L $B/helloandroid/souffle/lib/*/*.so | grep -c 'ARM aarch64\|x86-64' | grep -qx 2"
    step helloandroid.compare bash -c "python3 $A/baseline/commands.py compare --root NDK=$ANDROID_NDK_ROOT \
        --root PREFIX=$BUILDLOG_HALIDE_PREFIX --root SRC=$PWD/$P/halide/upstream/apps/HelloAndroid \
        --root BUILD=/nonexistent $AB/cmake-helloandroid.json $B/helloandroid/souffle | tee /out/helloandroid-compare-$FAMILY.txt \
        | grep -q '^generate  baseline   4  buildlog   4  same key   4  identical   4 '"
fi

exit $status
