# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Halide case study: the runtime (CMake's Halide_initmod).

halide-build compiles each runtime module to LLVM IR with the bound Clang,
assembles it with LLVM's llvm-as and embeds it with binary2cpp into
libHalide. The tools are named by reference: the imported Clang::clang and
LLVM::llvm-as, and the built binary2cpp, all in the exec configuration by
inference. No object comes from the CMake tree any more. Needs the pinned
Halide export and the CMake release tree (libhalide/buildlog.toml's [vars])
and Homebrew's llvm@22.
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

from .halide_env import (
    CMAKE_TREE,
    HALIDE_SRC,
    cmake_source_dir,
    compare_env,
    configure_libhalide,
)

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "halide"
LIBHALIDE = CASE / "libhalide"
PACKAGES = CASE / "packages"
SRC = HALIDE_SRC
REL = "arm64-macos.release.none.none"
CFG = "[arm64-macos, [[native.opt, release], [[native.sanitize, none], [[native.linkage, default], [[python.interpreter, none], nil]]]]]"
RT = f"gen/{REL}/Halide/runtime"

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "src/runtime/CMakeLists.txt").exists()
        or not (CMAKE_TREE / "src/runtime").is_dir(),
        reason="the pinned Halide export and its CMake release tree are not present",
    ),
    pytest.mark.skipif(
        not os.path.isdir("/opt/homebrew/opt/llvm@22"), reason="Homebrew llvm@22 not installed"
    ),
]


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


def test_runtime_tools_are_exec_by_inference(build):
    assert not (LIBHALIDE / "external-objects.toml").exists()
    tool_dep = rows(build / "souffle/facts/tool_dep.csv")
    assert {("Halide", CFG, "Clang::clang"), ("Halide", CFG, "LLVM::llvm-as")} <= tool_dep
    ninja = (build / "souffle/build.ninja").read_text()
    labels = re.findall(r"^  label = (\S+)$", ninja, re.MULTILINE)
    # CMake's counts (baseline/scope.md): 408 clang, 423 llvm-as, 15 copies,
    # 438 runtime embeds (+5 templates, +2 autoscheduler weights).
    assert labels.count("RUNTIME_CC") == 408
    assert labels.count("LLVM_AS") == 423
    assert labels.count("EMBED") == 445
    assert (
        len(
            re.findall(
                r"-m buildlog -E copy \S+/src/runtime/\S+\.ll \S+\.ll\.transformed\.ll", ninja
            )
        )
        == 15
    )
    link = next(
        line
        for line in ninja.splitlines()
        if line.startswith(f"build lib/{REL}/libHalide.22.0.0.dylib:")
    )
    assert len(re.findall(rf"obj/{REL}/Halide/{RT}/_initmod_\S+\.cpp\.o", link)) == 438
    assert "cmake-release" not in link
    # The clang compile: the bound Clang, a data triple, a depfile, no cwd;
    # llvm-as and binary2cpp run in the runtime directory.
    cc = re.search(
        rf"build {RT}/initmod\.windows_clock_64_debug\.ll: .*\n  label = RUNTIME_CC\n  cmd = (.*)\n  dep = (.*)\n",
        ninja,
    )
    assert (
        cc
        and "/llvm-install/homebrew-llvm-22/bin/clang-" in cc[1].split()[0]
        and "/bin/clang-" in cc[1].split()[0]
    )
    assert " -g -DDEBUG_RUNTIME -DCOMPILING_HALIDE_RUNTIME -DBITS_64 " in cc[1]
    assert " -fshort-wchar " in cc[1] and " -fpic " not in cc[1]
    assert " -m64 -target x86_64-unknown-windows-unknown -emit-llvm -S -MD -MF " in cc[1]
    assert cc[2] == f"{RT}/initmod.windows_clock_64_debug.d"
    assert (
        f"cmd = cd {RT} && " in ninja
        and "llvm-as initmod.cache_32.ll -o initmod.cache_32.bc\n" in ninja
    )
    assert (
        "binary2cpp halide_internal_initmod_webgpu_dawn_32 < initmod.webgpu_dawn_32.bc > _initmod_webgpu_dawn_32.cpp\n"
        in ninja
    )
    assert " -m32 -target wasm32-unknown-unknown-unknown " in ninja


def test_runtime_outputs_match_cmakes(build):
    ninja_dir = build / "souffle"
    exact = ["_initmod_aarch64.cpp", "_initmod_HalideBuffer_h.cpp", "_initmod_inlined_c.cpp",
             "_initmod_ptx_libdevice.cpp"]  # fmt: skip
    ir = ["initmod.alignment_128_32.ll", "initmod.cache_64.ll"]
    subprocess.run(
        ["ninja", "-C", str(ninja_dir), *(f"{RT}/{f}" for f in exact + ir)],
        check=True,
        capture_output=True,
    )
    for f in exact:  # no source path inside: byte-identical
        assert (ninja_dir / RT / f).read_bytes() == (CMAKE_TREE / "src/runtime" / f).read_bytes(), f
    # The compiled IR differs only in how the source path is spelled (buildlog:
    # relative to the build directory; CMake: absolute), which reaches the
    # module id and __FILE__ strings.
    rel = os.path.relpath(SRC, ninja_dir) + "/"

    def norm(text):
        return re.sub(r"\[\d+ x i8\] c\"", '[N x i8] c"', text)

    for f in ir:
        ours = (ninja_dir / RT / f).read_text().replace(rel, f"{SRC}/")
        theirs = (CMAKE_TREE / "src/runtime" / f).read_text().replace(cmake_source_dir(), f"{SRC}")
        assert norm(ours) == norm(theirs), f


def test_phase3_comparison(build, tmp_path):
    out = tmp_path / "cmp.json"
    subprocess.run(
        [sys.executable, str(CASE / "phase1/compare_libhalide.py"), str(build), "--phase", "1",
         "--phase", "2", "--phase", "3", "--details", "0", "--json", str(out)],
        check=False, capture_output=True, env=compare_env(),
    )  # fmt: skip
    doc = json.loads(out.read_text())
    counts = {(r["phase"], r["kind"]): r for r in doc["rows"]}
    # Every llvm-as and binary2cpp run matches CMake's exactly (with its
    # working directory and redirections).
    assert counts[(3, "custom")] == {"phase": 3, "kind": "custom", "match": 861}
    assert counts[(3, "compile")] == {"phase": 3, "kind": "compile", "differ": 846}
    assert counts[(3, "copy")] == {"phase": 3, "kind": "copy", "differ": 15}
    assert counts[(2, "custom")] == {"phase": 2, "kind": "custom", "match": 6}
    assert counts[(1, "compile")] == {"phase": 1, "kind": "compile", "differ": 188}
