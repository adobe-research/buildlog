# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Halide case study: the serialization header (CMake's flatc step).

halide-build runs the imported flatbuffers::flatc, named by reference (exec
by inference), on src/halide_ir.fbs into a generated include directory, which
reaches libHalide's compiles through a private interface dependency. After
this phase the libHalide graph takes nothing from the CMake build tree. Needs
the pinned Halide export, Homebrew's llvm@22 and flatbuffers; the CMake
release tree only for the byte and graph comparisons.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from buildlog.paths import souffle_path

from .halide_env import CMAKE_TREE, HALIDE_SRC, compare_env, configure_libhalide

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "halide"
LIBHALIDE = CASE / "libhalide"
PACKAGES = CASE / "packages"
SRC = HALIDE_SRC
REL = "arm64-macos.release.none.none"
CFG = "[arm64-macos, [[native.opt, release], [[native.sanitize, none], [[native.linkage, default], [[python.interpreter, none], nil]]]]]"
HEADER = f"include/{REL}/flatc/halide_ir.fbs.h"

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "src/halide_ir.fbs").exists(), reason="the pinned Halide export is not present"
    ),
    pytest.mark.skipif(
        not os.path.isdir("/opt/homebrew/opt/llvm@22")
        or not os.path.exists("/opt/homebrew/bin/flatc"),
        reason="Homebrew llvm@22 or flatbuffers not installed",
    ),
]
needs_cmake_tree = pytest.mark.skipif(
    not (CMAKE_TREE / "include/flatc").is_dir(), reason="the CMake release tree is not present"
)


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    os.environ["BUILDLOG_PACKAGE_PATH"] = str(PACKAGES)
    try:
        build, metrics = configure_libhalide(tmp_path_factory)
    finally:
        del os.environ["BUILDLOG_PACKAGE_PATH"]
    assert metrics["errors"] == []
    return build


def rows(path):
    return {tuple(line.split("\t")) for line in path.read_text().splitlines() if line}


def test_flatc_is_exec_by_inference_and_nothing_comes_from_cmake(build):
    assert ("Halide", CFG, "flatbuffers::flatc") in rows(build / "souffle/facts/tool_dep.csv")
    ninja = (build / "souffle/build.ninja").read_text()
    assert "cmake-release" not in ninja
    edge = re.search(
        rf"^build {re.escape(HEADER)}: .*\n  label = FLATC\n  cmd = (.*)$", ninja, re.MULTILINE
    )
    assert edge
    assert edge[1].startswith(f"cd gen/{REL}/Halide && /opt/homebrew/bin/flatc --cpp ")
    src = os.path.relpath(SRC, build / "souffle" / "gen" / REL / "Halide")
    assert edge[1].endswith(f" -o ../../../include/{REL}/flatc {src}/src/halide_ir.fbs")
    ser = re.search(rf"^build obj/{REL}/Halide/src/Serialization\.cpp\.o: (.*)\n(?:  .*\n)*?  cmd = (.*)$", ninja, re.MULTILINE)  # fmt: skip
    assert ser and HEADER in ser[1].split("||")[1]
    assert f" -isystem include/{REL}/flatc " in ser[2]


@needs_cmake_tree
def test_flatc_header_matches_cmakes(build):
    ninja_dir = build / "souffle"
    subprocess.run(["ninja", "-C", str(ninja_dir), HEADER], check=True, capture_output=True)
    ours = (ninja_dir / HEADER).read_bytes()
    assert ours == (CMAKE_TREE / "include/flatc/halide_ir.fbs.h").read_bytes()


@needs_cmake_tree
def test_phase5_comparison(build, tmp_path):
    out = tmp_path / "cmp.json"
    subprocess.run(
        [sys.executable, str(CASE / "phase1/compare_libhalide.py"), str(build), "--phase", "1",
         "--phase", "5", "--details", "0", "--json", str(out)],
        check=False, capture_output=True, env=compare_env(),
    )  # fmt: skip
    doc = json.loads(out.read_text())
    counts = {(r["phase"], r["kind"]): r for r in doc["rows"]}
    # flatc's run matches CMake's exactly, working directory included.
    assert counts[(5, "custom")] == {"phase": 5, "kind": "custom", "match": 1}
    assert counts[(5, "compile")] == {"phase": 5, "kind": "compile", "differ": 2}
    assert counts[(1, "compile")] == {"phase": 1, "kind": "compile", "differ": 188}
