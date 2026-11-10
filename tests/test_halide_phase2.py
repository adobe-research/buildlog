# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Halide case study: libHalide's host tools and generated sources.

binary2cpp, build_halide_h and regexp_replace are ordinary executables in the
spec; the actions of halide-build's generated sources name them by reference,
so core builds them for the exec configuration. Nothing in the spec or the
package says "host" or "exec". Needs the pinned Halide export and the CMake
release tree (libhalide/buildlog.toml's [vars]) and Homebrew's llvm@22.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from buildlog.cli import main
from buildlog.paths import souffle_path

from .halide_env import CMAKE_TREE, HALIDE_SRC, compare_env, configure_libhalide, project_copy

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "halide"
LIBHALIDE = CASE / "libhalide"
PACKAGES = CASE / "packages"
SRC = HALIDE_SRC
REL = "arm64-macos.release.none.none"
CFG = "[arm64-macos, [[native.opt, release], [[native.sanitize, none], [[native.linkage, default], [[python.interpreter, none], nil]]]]]"

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "tools/binary2cpp.cpp").exists() or not (CMAKE_TREE / "include").is_dir(),
        reason="the pinned Halide export and its CMake release tree are not present",
    ),
    pytest.mark.skipif(
        not os.path.isdir("/opt/homebrew/opt/llvm@22"), reason="Homebrew llvm@22 not installed"
    ),
]

TOOLS = ("binary2cpp", "build_halide_h", "regexp_replace")


@pytest.fixture(autouse=True)
def _package_path(monkeypatch):
    monkeypatch.setenv("BUILDLOG_PACKAGE_PATH", str(PACKAGES))


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


def test_no_exec_annotations_in_the_spec_or_package():
    spec = "\n".join(p.read_text() for p in LIBHALIDE.glob("*.toml"))
    pkg = PACKAGES / "share/buildlog/halide-build"
    # Every layer of the package.
    rules = [f.read_text() for f in sorted(pkg.glob("*.dl"))]
    assert len(rules) >= 4
    reads = []
    for i, text in enumerate((spec, *rules)):
        code = "\n".join(re.sub(r"(#|//).*", "", line) for line in text.splitlines())
        if i:
            # String values in the package's rules (Halide's own
            # target name "host", error messages) name no relation, and
            # *reads* of core's derived exec_config/tool_dep in a rule body
            # are allowed: is this config built on the platform its tools
            # run on? which Clang does a config run, to check it
            # against the LLVM it links? Neither states what
            # runs where.
            code = re.sub(r'"[^"]*"', '""', code)
            for rd in (
                r"^halide_build_same_platform\(c\) :- exec_config\(c, e\)",
                r"^  tool_dep\(t, c, \"\"\), exec_config\(c, e\)",
            ):
                reads += re.findall(rd, code, re.MULTILINE)
                code = re.sub(rd, "", code, flags=re.MULTILINE)
        assert not re.search(r"tool_dep|exec_config|\bexec\b|\bhost\b", code)
    assert len(reads) == 1


def test_tools_are_built_for_exec_by_inference(build):
    facts = build / "souffle" / "facts"
    # (The runtime also runs the imported Clang::clang and
    # LLVM::llvm-as: tests/test_halide_phase3.py.)
    assert {
        ("Halide", CFG, "binary2cpp"),
        ("Halide", CFG, "build_halide_h"),
    } <= rows(facts / "tool_dep.csv")
    needed = rows(facts / "needed.csv")
    assert {(t, CFG) for t in TOOLS} <= needed
    ninja = (build / "souffle" / "build.ninja").read_text()
    # The five templates: binary2cpp SYMBOL < template > file, compiled into
    # libHalide, run in the generated sources' directory (CMake's build/src).
    embeds = re.findall(rf"cmd = cd gen/{REL}/Halide && (\S+/binary2cpp \S+ < \S+ > \S+)\n", ninja)
    assert len(embeds) == 5
    assert all(e.startswith(f"../../../bin/{REL}/binary2cpp halide_") for e in embeds)
    link = next(
        line
        for line in ninja.splitlines()
        if line.startswith(f"build lib/{REL}/libHalide.22.0.0.dylib:")
    )
    assert (
        len(re.findall(rf"obj/{REL}/Halide/gen/{REL}/Halide/\S+_template\.\S+\.cpp\.o", link)) == 5
    )
    assert "CMakeFiles/Halide.dir" not in link  # no template object from the CMake tree
    # Halide.h: build_halide_h (run in src/) > Halide.h.tmp, then copy_if_different
    # with restat, as CMake does.
    mono = re.search(r"cmd = cd (\S+) && (\S+/build_halide_h) .* > (\S+)\n", ninja)
    assert mono and mono[1].endswith("/src") and mono[2].endswith(f"/bin/{REL}/build_halide_h")
    assert mono[3].endswith(f"/include/{REL}/Halide.h.tmp")
    assert (
        f"-m buildlog -E copy_if_different include/{REL}/Halide.h.tmp include/{REL}/Halide.h\n"
        "  restat = 1\n"
    ) in ninja


def test_generated_sources_match_cmakes(build):
    ninja_dir = build / "souffle"
    gen = sorted(
        f"gen/{REL}/Halide/{p.name}" for p in (CMAKE_TREE / "src").glob("*_template.*.cpp")
    )
    assert len(gen) == 5
    subprocess.run(
        ["ninja", "-C", str(ninja_dir), f"include/{REL}/Halide.h", *gen],
        check=True,
        capture_output=True,
    )
    assert (ninja_dir / f"include/{REL}/Halide.h").read_bytes() == (
        CMAKE_TREE / "include/Halide.h"
    ).read_bytes()
    for g in gen:
        assert (ninja_dir / g).read_bytes() == (CMAKE_TREE / "src" / Path(g).name).read_bytes(), g


def test_why_explains_binary2cpps_configuration(build, capsys):
    capsys.readouterr()
    fact = 'needed("binary2cpp", ["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], [["native.linkage", "default"], [["python.interpreter", "none"], nil]]]]])'
    assert main(["why", "-C", str(build), fact, str(project_copy(build))]) == 0
    out = capsys.readouterr().out
    assert f'tool_dep("Halide", ‹{REL}›, "binary2cpp")' in out or (
        f'tool_dep("adams2019_weights_obj", ‹{REL}›, "binary2cpp")' in out
    )
    assert '"binary2cpp", "run")' in out  # the action_run reference
    assert "halide_build_embed(" in out
    assert "libhalide/spec.toml:" in out or "libhalide/autoschedulers.toml:" in out


def test_phase2_comparison(build, tmp_path):
    out = tmp_path / "cmp.json"
    subprocess.run(
        [sys.executable, str(CASE / "phase1/compare_libhalide.py"), str(build), "--phase", "2",
         "--details", "0", "--json", str(out)],
        check=False, capture_output=True, env=compare_env(),
    )  # fmt: skip
    doc = json.loads(out.read_text())
    counts = {(r["phase"], r["kind"]): r for r in doc["rows"]}
    # All six custom commands match CMake's exactly, with their redirections
    # and working directories (Halide.h: the generate + copy_if_different pair).
    assert counts[(2, "custom")] == {"phase": 2, "kind": "custom", "match": 6}
    assert all(counts[(2, k)].get("only_b", 0) == 0 for k in ("compile", "custom", "link"))
    assert all(counts[(2, k)].get("only_a", 0) == 0 for k in ("compile", "custom", "link"))
