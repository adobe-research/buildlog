#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Build nanobind_example's wheel through a PEP 517 frontend (build) with buildlog-python's PEP 517
# backend (the nanobind case study; ../README.md, "Wheels"), for each
# interpreter TAG, install it in a fresh venv and run upstream's tests.
#
#   run-pep517.sh [TAG ...]        TAG in: 313 (default), 311
#
# The project is a scratch copy of the pinned checkout whose pyproject.toml
# is ../pyproject/pyproject.toml with the spec path made absolute and the
# source root "." (the author's tree). build runs without isolation in
# $CASE_DIR/venv<TAG> (nanobind is installed there); buildlog-python's
# sources are put on PYTHONPATH, since they are not published, and buildlog
# itself runs from this repository through uv (buildlog.command).
set -euo pipefail
export PIP_DISABLE_PIP_VERSION_CHECK=1
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
CASE_DIR=${CASE_DIR:-$REPO/build/case-nanobind}
CHECKOUT=${BUILDLOG_NANOBIND_EXAMPLE_SRC:-$HERE/../upstream}
PIN=03bd872fdb8f34f7ddb68ced81e4dc2ce08283c7
TAGS=("$@")
[[ ${#TAGS[@]} -gt 0 ]] || TAGS=(313 311)
[[ $(git -C "$CHECKOUT" rev-parse HEAD) == "$PIN" ]] || { echo "checkout is not at $PIN" >&2; exit 1; }

for TAG in "${TAGS[@]}"; do
  PY=$CASE_DIR/venv$TAG/bin/python
  WORK=$CASE_DIR/scratch/pep517-$TAG
  rm -rf "$WORK" && mkdir -p "$WORK/src" "$WORK/dist"
  git -C "$CHECKOUT" archive HEAD | tar -x -C "$WORK/src"
  sed -e "s|^spec = \[\"../project/spec.toml\"\]|spec = [\"$REPO/case-studies/nanobind/project/spec.toml\"]|" \
      -e 's|^source_root = .*|source_root = "."|' \
      "$REPO/case-studies/nanobind/pyproject/pyproject.toml" > "$WORK/src/pyproject.toml"
  (cd "$WORK/src" && PYTHONPATH=$REPO/case-studies/nanobind/plugins/buildlog-python/src \
    "$PY" -m build --wheel --no-isolation --skip-dependency-check -o "$WORK/dist" \
      -C "buildlog.command=uv run --frozen --project $REPO buildlog" \
      -C "buildlog.user=$REPO/case-studies/nanobind/project/presets/apple-clang-arm64.toml" \
      -C "buildlog.package-path=$REPO/case-studies/nanobind" \
      -C "build-dir=$WORK/build" .) > "$WORK/build.log" 2>&1 || { cat "$WORK/build.log" >&2; exit 1; }
  WHEEL=$(ls "$WORK/dist"/*.whl)
  "$PY" -m venv "$WORK/testvenv"
  "$WORK/testvenv/bin/python" -m pip install -q "$WHEEL" pytest
  OUT=$HERE/$TAG
  mkdir -p "$OUT"
  basename "$WHEEL" > "$OUT/wheel-name.txt"
  "$WORK/testvenv/bin/python" -m zipfile -l "$WHEEL" | awk 'NR>1 {print $1}' | sort > "$OUT/wheel-files.txt"
  "$WORK/testvenv/bin/python" -m zipfile -e "$WHEEL" "$WORK/unpacked"
  cp "$WORK"/unpacked/*.dist-info/WHEEL "$OUT/WHEEL"
  (cd "$CHECKOUT" && "$WORK/testvenv/bin/python" -m pytest -q -p no:cacheprovider -o addopts= tests) \
    | sed -e 's/ in [0-9.]*s/ in <t>/' > "$OUT/pytest.txt"
  echo "pep517 $TAG: $(cat "$OUT/wheel-name.txt")"
done
