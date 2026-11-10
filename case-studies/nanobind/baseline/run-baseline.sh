#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The nanobind case study's ground truth: nanobind_example built by its own
# build backend (scikit-build-core driving CMake and Ninja), once per selected
# interpreter, the wheel's contents and tags, and the example's pytest run
# against the installed wheel.
#
#   run-baseline.sh [TAG ...]      TAG in: 313 (default config: CPython 3.13, abi3), 311
#
# Environment overrides:
#   NANOBIND_EXAMPLE_SRC  the nanobind_example checkout (default
#                         ../upstream, the submodule, pinned below). Only read.
#   CASE_DIR              where the venvs and raw trees live (default
#                         <repo>/build/case-nanobind, which is also where the
#                         project's NANOBIND_VENVS variable looks for the venvs).
#   BUILDLOG_NANOBIND_VENVS  where venv<TAG> are, if not in CASE_DIR (the Linux
#                         image has them in /opt/nanobind-venvs).
#
# On Linux (BASELINE_FAMILY gcc, default, or clang: CC/CXX of the build, which
# scikit-build-core takes from sysconfig otherwise) the files go to linux/<TAG>
# or linux-clang/<TAG>, with ELF's tools for the extension's report.
#
# Each TAG needs a venv $CASE_DIR/venv<TAG> holding the pinned build requirements
# (scikit-build-core, nanobind, build, pytest; see environment.txt). The build runs
# without isolation so those exact versions are used. Raw trees go to
# $CASE_DIR/baseline-raw/<TAG>; the committed files next to this script are
# normalized ($SRC, $BUILD, $NB for nanobind's package dir, $PY for the interpreter prefix).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CASE_DIR="${CASE_DIR:-$HERE/../../../build/case-nanobind}"
VENVS="${BUILDLOG_NANOBIND_VENVS:-$CASE_DIR}"
SRC="$(cd "${NANOBIND_EXAMPLE_SRC:-$HERE/../upstream}" && pwd)"
PIN=03bd872fdb8f34f7ddb68ced81e4dc2ce08283c7
OS="$(uname -s)"
SUB=""
if [[ "$OS" == Linux ]]; then
    SUB=linux
    if [[ "${BASELINE_FAMILY:-gcc}" == clang ]]; then
        SUB=linux-clang
        export CC=clang CXX=clang++
    else
        export CC=gcc CXX=g++
    fi
fi
TAGS=("$@")
[[ ${#TAGS[@]} -gt 0 ]] || TAGS=(313 311)

if [[ "$(git -C "$SRC" rev-parse HEAD)" != "$PIN" ]]; then
    echo "run-baseline.sh: $SRC is not at $PIN" >&2
    exit 1
fi
if [[ -n "$(git -C "$SRC" status --porcelain)" ]]; then
    echo "run-baseline.sh: $SRC has local changes or untracked files" >&2
    exit 1
fi

for tag in "${TAGS[@]}"; do
    VENV="$VENVS/venv$tag"
    PYEXE="$VENV/bin/python"
    RAW="$CASE_DIR/baseline-raw/$tag"
    BUILD="$RAW/build"
    OUT="$HERE/${SUB:+$SUB/}$tag"
    NB="$("$PYEXE" -c 'import nanobind, os; print(os.path.dirname(nanobind.__file__))')"
    PYPREFIX="$("$PYEXE" -c 'import sys; print(sys.base_prefix)')"
    rm -rf "$RAW" "$OUT"
    mkdir -p "$RAW" "$OUT"
    norm() {
        sed -e "s#$BUILD#\$BUILD#g" -e "s#$NB#\$NB#g" -e "s#$SRC#\$SRC#g" \
            -e "s#$VENV#\$VENV#g" -e "s#$PYPREFIX#\$PY#g" -e "s#$(realpath "$PYPREFIX")#\$PY#g"
    }
    {
        echo "# Environment ($tag)"
        uname -sm
        if [[ "$OS" == Linux ]]; then (. /etc/os-release && echo "$PRETTY_NAME"); else sw_vers -productVersion; fi
        echo "python $("$PYEXE" -c 'import sys; print(sys.version.split()[0], sys.implementation.cache_tag)')"
        "$PYEXE" -c 'import sysconfig as s; print("EXT_SUFFIX", s.get_config_var("EXT_SUFFIX")); print("platform", s.get_platform())'
        for p in scikit-build-core nanobind build cmake ninja pytest; do
            echo "$p $("$PYEXE" -c "import importlib.metadata as m; print(m.version('$p'))")"
        done
        echo "nanobind_example $PIN"
        "${CXX:-cc}" --version | head -1
    } > "$OUT/environment.txt"

    # The backend's own build: `python -m build --wheel` (PEP 517), no isolation.
    (cd "$SRC" && "$PYEXE" -m build --wheel --no-isolation --outdir "$RAW/dist" \
        -Cbuild-dir="$BUILD" -Cbuild.verbose=true \
        -Ccmake.define.CMAKE_EXPORT_COMPILE_COMMANDS=ON) > "$RAW/build.log" 2>&1
    # Remove what the build leaves in the checkout (egg-info style metadata, none expected).
    git -C "$SRC" status --porcelain > "$RAW/checkout-after-build.txt"

    ninja -C "$BUILD" -t commands | norm > "$OUT/commands.txt"
    norm < "$BUILD/compile_commands.json" > "$OUT/compile_commands.json"
    grep -E '^(Python_|nanobind_|SKBUILD|CMAKE_BUILD_TYPE|CMAKE_OSX|CMAKE_INSTALL_PREFIX|CMAKE_MAKE_PROGRAM|CMAKE_CXX_COMPILER:)' \
        "$BUILD/CMakeCache.txt" | norm | sed -E 's#/[^ ]*/(T|tmp)/tmp[a-z0-9_]+#$WHEELTMP#g' | sort > "$OUT/cmake-cache.txt"
    # What scikit-build-core passed to CMake: its initial cache file (-C), and
    # the backend's log (the build lines are commands.txt).
    norm < "$BUILD/CMakeInit.txt" | sed -E 's#/[^ ]*/(T|tmp)/tmp[a-z0-9_]+#$WHEELTMP#g' > "$OUT/cmake-init.txt"
    grep -v -E '^\[[0-9]+/[0-9]+\]' "$RAW/build.log" | norm | sed -E 's#/[^ ]*/(T|tmp)/tmp[a-z0-9_]+#$WHEELTMP#g' > "$OUT/backend-log.txt"

    WHL="$(ls "$RAW"/dist/*.whl)"
    basename "$WHL" > "$OUT/wheel-name.txt"
    mkdir -p "$RAW/whl" && "$PYEXE" -m zipfile -e "$WHL" "$RAW/whl"
    (cd "$RAW/whl" && find . -type f | sort) > "$OUT/wheel-files.txt"
    cp "$RAW"/whl/*.dist-info/WHEEL "$OUT/WHEEL"
    cp "$RAW"/whl/*.dist-info/METADATA "$OUT/METADATA"
    sed -E 's/,sha256=[^,]*,[0-9]+$/,<hash>,<size>/' "$RAW"/whl/*.dist-info/RECORD > "$OUT/RECORD"
    SO="$(ls "$RAW"/whl/nanobind_example/*.so)"
    if [[ "$OS" == Linux ]]; then
        {
            echo "===== nm -gD --defined-only $(basename "$SO")"
            nm -gD --defined-only "$SO"
            echo "===== undefined Python symbols (nm -D -u | grep Py)"
            nm -D -u "$SO" | awk '{print $2}' | grep -E '^_?_?Py' | sort
            echo "===== readelf -d NEEDED"
            readelf -d "$SO" | grep NEEDED
            echo "===== size"
            stat -c %s "$SO"
        } | norm > "$OUT/extension.txt"
    else
        {
            echo "===== nm -gU $(basename "$SO")"
            nm -gU "$SO"
            echo "===== undefined Python symbols (nm -u | grep _Py)"
            nm -u "$SO" | grep -E '^_Py|^__Py' | sort
            echo "===== otool -L"
            otool -L "$SO" | tail -n +2
            echo "===== LC_BUILD_VERSION"
            otool -l "$SO" | grep -A4 LC_BUILD_VERSION | grep -E 'minos|sdk'
            echo "===== size"
            stat -f %z "$SO"
        } | norm > "$OUT/extension.txt"
    fi
    # The example's tests against the installed wheel, in a fresh venv.
    TVENV="$RAW/test-venv"
    uv venv -q -p "$PYEXE" "$TVENV"
    VIRTUAL_ENV="$TVENV" uv pip install -q "$WHL" pytest
    (cd "$RAW" && "$TVENV/bin/python" -m pytest -q "$SRC/tests" -p no:cacheprovider -o addopts=) \
        2>&1 | grep -E 'passed|failed|error' | sed -E 's/ in [0-9.]+s//' > "$OUT/pytest.txt"
    echo "baseline $tag written to $OUT"
done
