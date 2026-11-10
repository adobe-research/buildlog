# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Halide case study: the generator tooling and the autoschedulers.

tools/'s GenGen, Generator (a whole archive) and gengen; the autoschedulers'
plugins (module libraries), their helper library and tools; the cost models,
which a generator built in the graph writes (add_halide_library) with runtimes
gengen writes (add_halide_runtime). The generators and gengen are named by
reference, so they (and libHalide they link) are built for the exec
configuration without an annotation. Needs the pinned Halide export and
Homebrew's llvm@22 and flatbuffers; the comparison also needs the baseline.
BUILDLOG_HALIDE_BUILD=1 also builds the graph and loads the plugins.
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

from .halide_env import HALIDE_SRC, compare_env, configure_libhalide, project_copy

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "halide"
LIBHALIDE = CASE / "libhalide"
PACKAGES = CASE / "packages"
SRC = HALIDE_SRC
REL = "arm64-macos.release.none.none"
CFG = "[arm64-macos, [[native.opt, release], [[native.sanitize, none], [[native.linkage, default], [[python.interpreter, none], nil]]]]]"
PLUGINS = ("adams2019", "anderson2021", "li2018", "mullapudi2016")

pytestmark = [
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "src/autoschedulers/adams2019/baseline.weights").exists(),
        reason="the pinned Halide export is not present",
    ),
    pytest.mark.skipif(
        not os.path.isdir("/opt/homebrew/opt/llvm@22")
        or not os.path.exists("/opt/homebrew/bin/flatc"),
        reason="Homebrew llvm@22 or flatbuffers not installed",
    ),
]


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


def cmd_of(ninja: str, out: str) -> str:
    m = re.search(rf"^build {re.escape(out)}[ :](?:.*\n)*?  cmd = (.*)$", ninja, re.MULTILINE)
    assert m, out
    return m[1]


def test_generators_and_gengen_are_exec_by_inference(build):
    facts = build / "souffle" / "facts"
    tool_dep = rows(facts / "tool_dep.csv")
    for m in ("adams2019", "anderson2021"):
        gen = f"{m}_cost_model.generator"
        assert {
            (f"{m}_cost_model", CFG, gen),
            (f"{m}_train_cost_model", CFG, gen),
            (f"{m}_cost_model.runtime", CFG, "gengen"),
            (f"{m}_weights_obj", CFG, "binary2cpp"),
        } <= tool_dep
    ninja = (build / "souffle" / "build.ninja").read_text()
    # The generator runs in its library's directory, for the library's
    # configuration's Halide target (not where it runs), without a runtime.
    lib = f"gen/{REL}/adams2019_train_cost_model"
    assert cmd_of(ninja, f"{lib}/adams2019_train_cost_model.o") == (
        f"cd {lib} && ../../../bin/{REL}/adams2019_cost_model.generator -n "
        "adams2019_train_cost_model -d 0 -g train_cost_model -f train_cost_model "
        "-e c_header,object -o . target=arm-64-macos-no_runtime"
    )
    rt = f"gen/{REL}/adams2019_cost_model.runtime"
    assert cmd_of(ninja, f"{rt}/adams2019_cost_model.runtime.o") == (
        f"cd {rt} && ../../../bin/{REL}/gengen -r adams2019_cost_model.runtime -o . "
        "-e object target=arm-64-macos"
    )
    # The generator links GenGen whole (Halide::Generator) and libHalide,
    # with a build-tree rpath.
    link = cmd_of(ninja, f"bin/{REL}/adams2019_cost_model.generator")
    assert link.count("libHalide_GenGen.a") == 1
    assert (
        f" -Wl,-force_load,lib/{REL}/libHalide_GenGen.a lib/{REL}/libHalide.22.0.0.dylib " in link
    )
    assert link.endswith(f" -Wl,-rpath,@loader_path/../../lib/{REL}")
    # A plugin is a bundle of its objects, the weights' object, the cost
    # models with their runtime, libHalide and ASLog.
    so = cmd_of(ninja, f"lib/{REL}/libautoschedule_adams2019.so")
    assert " -bundle " in so
    for item in (
        f"obj/{REL}/adams2019_weights_obj/gen/{REL}/adams2019_weights_obj/baseline.cpp.o",
        f"lib/{REL}/libadams2019_cost_model.a",
        f"lib/{REL}/libadams2019_train_cost_model.a",
        f"lib/{REL}/libadams2019_cost_model.runtime.a",
        f"lib/{REL}/libHalide.22.0.0.dylib",
        f"lib/{REL}/libHalide_ASLog.a",
    ):
        assert f" {item} " in so, item
    # Consumers of a generated library get its header's directory and wait
    # for it; the runtime's headers come through Halide::Runtime.
    comp = re.search(
        rf"^build obj/{REL}/autoschedule_adams2019/src/autoschedulers/adams2019/"
        r"DefaultCostModel\.cpp\.o: (.*)\n(?:  .*\n)*?  cmd = (.*)$",
        ninja,
        re.MULTILINE,
    )
    assert comp and f"{lib}/adams2019_train_cost_model.h" in comp[1].split("||")[1]
    assert f" -isystem {lib} " in comp[2] and " -fPIC " in comp[2] and "/src/runtime " in comp[2]


def test_why_explains_a_generators_configuration(build, capsys):
    capsys.readouterr()
    fact = (
        'needed("adams2019_cost_model.generator", ["arm64-macos", [["native.opt", "release"], '
        '[["native.sanitize", "none"], [["native.linkage", "default"], [["python.interpreter", "none"], nil]]]]])'
    )
    assert main(["why", "-C", str(build), fact, str(project_copy(build))]) == 0
    out = capsys.readouterr().out
    assert f'tool_dep("adams2019_cost_model", ‹{REL}›, "adams2019_cost_model.generator")' in out
    assert '"adams2019_cost_model.generator", "run")' in out
    assert "halide_build_library(" in out and "libhalide/autoschedulers.toml:" in out
    assert "exec_config(" in out
    # The Halide target is the library's configuration's; the rule is cited
    # at its clause (the target list has a clause of its own).
    layer = PACKAGES / "share/buildlog/halide-build/halide_build_generator.dl"
    line = next(
        i
        for i, text in enumerate(layer.read_text().splitlines(), 1)
        if text.startswith('action_arg(o, r + 15, "target=", s)')
    )
    assert (
        main([
            "why", "-C", str(build),
            "--flag", "target=arm-64-macos-no_runtime",
            "--output", f"gen/{REL}/adams2019_cost_model/adams2019_cost_model.o",
            str(project_copy(build)),
        ])
        == 0
    )  # fmt: skip
    out = capsys.readouterr().out
    assert f"[halide-build:halide_build_generator.dl:{line}]" in out.splitlines()[0]
    assert 'halide_build_arch("arm64", "arm-64")' in out


def test_phase4_comparison(build, tmp_path):
    out = tmp_path / "cmp.json"
    subprocess.run(
        [sys.executable, str(CASE / "phase1/compare_libhalide.py"), str(build), "--phase", "4",
         "--details", "0", "--json", str(out)],
        check=False, capture_output=True, env=compare_env(),
    )  # fmt: skip
    doc = json.loads(out.read_text())
    counts = {(r["phase"], r["kind"]): r for r in doc["rows"]}
    # Every CMake command of the generator tooling has a buildlog counterpart except the
    # tutorial's win32 runtime (gengen's, out of scope); the embeds match.
    assert counts[(4, "custom")] == {
        "phase": 4,
        "kind": "custom",
        "match": 2,
        "differ": 6,
        "only_a": 1,
    }
    assert counts[(4, "compile")] == {"phase": 4, "kind": "compile", "differ": 37}
    assert counts[(4, "link")] == {"phase": 4, "kind": "link", "differ": 13}
    assert counts[(4, "archive")] == {"phase": 4, "kind": "archive", "differ": 8}
    assert counts[(4, "phony")] == {"phase": 4, "kind": "phony", "only_a": 84}


@pytest.mark.skipif(
    not os.environ.get("BUILDLOG_HALIDE_BUILD"), reason="set BUILDLOG_HALIDE_BUILD=1 to build"
)
def test_build_runs_generators_and_loads_plugins(build, tmp_path):
    ninja_dir = build / "souffle"
    subprocess.run(["ninja", "-C", str(ninja_dir)], check=True, capture_output=True)
    for f in ("adams2019_cost_model.h", "adams2019_cost_model.o"):
        assert (ninja_dir / f"gen/{REL}/adams2019_cost_model" / f).is_file()
    gen = ninja_dir / f"bin/{REL}/adams2019_cost_model.generator"
    for p in PLUGINS:
        plugin = ninja_dir / f"lib/{REL}/libautoschedule_{p}.so"
        subprocess.run(
            [str(gen), "-g", "cost_model", "-e", "c_header", "-o", str(tmp_path), "-n", f"x_{p}",
             "target=host", "-p", str(plugin)],
            check=True, capture_output=True,
        )  # fmt: skip
        assert (tmp_path / f"x_{p}.h").is_file()
