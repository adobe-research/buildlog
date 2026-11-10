# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The bazel-flatc case study (case-studies/bazel-flatc/README.md).

Unit tests of the baseline tools (normalize_aquery.py, compare.py) on a small
synthetic aquery graph, checks of the checked-in baseline, and integration
tests that configure and build the buildlog project against the pinned
flatbuffers checkout ($BUILDLOG_BAZEL_FLATC_SRC, default the submodule
case-studies/bazel-flatc/upstream).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path

from . import host

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "bazel-flatc"
PROJECT = CASE / "project"
PRESET = PROJECT / "presets" / ("apple-clang.toml" if host.MACOS else f"{host.FAMILY}.toml")
BASELINE = CASE / "baseline"
# Linux has the native and opt variants: the cross variant's target is x86_64-macos.
VARIANTS = ("native", "opt", "cross") if host.MACOS else ("native", "opt")
if host.LINUX:
    BASELINE_DIR = BASELINE / ("linux" if host.FAMILY == "gcc" else "linux-clang")
else:
    BASELINE_DIR = BASELINE
SRC = Path(os.path.expanduser(os.environ.get("BUILDLOG_BAZEL_FLATC_SRC", str(CASE / "upstream"))))
HOST_PLATFORM = "arm64-macos" if host.MACOS else "arm64-linux"
EXEC = f"{HOST_PLATFORM}.release.none"
# sha256 of Bazel's monster_generated.h (baseline/bazel-*-evidence.txt).
HEADER_SHA = "b735c911b19b778f954531add4978e49894627793c465da8d4b84f84de2bb8ac"

needs_flatbuffers = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "src" / "flatc.cpp").exists(), reason="the pinned flatbuffers checkout is absent"
    ),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, BASELINE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


naq = _load("normalize_aquery")
cmp = _load("compare")


# --- a synthetic aquery graph: flatc linked in exec, run by a target genrule ---


def _graph() -> dict:
    frags, arts = [], []

    def path(p: str) -> int:
        parent = None
        for part in p.split("/"):
            hit = next(
                (f for f in frags if f["label"] == part and f.get("parentId") == parent), None
            )
            if hit is None:
                hit = {"id": len(frags) + 1, "label": part}
                if parent is not None:
                    hit["parentId"] = parent
                frags.append(hit)
            parent = hit["id"]
        arts.append({"id": len(arts) + 1, "pathFragmentId": parent})
        return len(arts)

    ex, tg = "darwin_arm64-opt-exec-ST-1", "darwin_x86_64-fastbuild"
    src = path("external/flatbuffers+/src/flatc.cpp")
    obj = path(f"bazel-out/{ex}/bin/external/flatbuffers+/src/_objs/flatc/flatc.o")
    flatc = path(f"bazel-out/{ex}/bin/external/flatbuffers+/flatc")
    fbs = path("monster.fbs")
    hdr = path(f"bazel-out/{tg}/bin/monster_generated.h")
    tc = path("external/apple_support+/crosstool/wrapped_clang")
    link = path(f"bazel-out/{tg}/bin/sample_binary")
    return {
        "pathFragments": frags,
        "artifacts": arts,
        "depSetOfFiles": [
            {"id": 1, "directArtifactIds": [src, tc]},
            {"id": 2, "directArtifactIds": [obj]},
            {"id": 3, "directArtifactIds": [fbs], "transitiveDepSetIds": [4]},
            {"id": 4, "directArtifactIds": [flatc]},
        ],
        "configuration": [
            {"id": 1, "mnemonic": ex, "platformName": "darwin_arm64", "isTool": True},
            {"id": 2, "mnemonic": tg, "platformName": "darwin_arm64"},
        ],
        "ruleClasses": [{"id": 1, "name": "cc_binary"}, {"id": 2, "name": "genrule"}],
        "targets": [
            {"id": 1, "label": "@@flatbuffers+//:flatc", "ruleClassId": 1},
            {"id": 2, "label": "//:monster_fbs_srcs", "ruleClassId": 2},
        ],
        "actions": [
            {
                "targetId": 1,
                "configurationId": 1,
                "mnemonic": "CppCompile",
                "arguments": [
                    "wrapped_clang_pp",
                    "-O2",
                    "-c",
                    "external/flatbuffers+/src/flatc.cpp",
                    "-o",
                    f"bazel-out/{ex}/bin/external/flatbuffers+/src/_objs/flatc/flatc.o",
                ],
                "inputDepSetIds": [1],
                "outputIds": [obj],
                "primaryOutputId": obj,
            },
            {
                "targetId": 1,
                "configurationId": 1,
                "mnemonic": "CppLink",
                "arguments": [
                    "cc_wrapper.sh",
                    "-o",
                    f"bazel-out/{ex}/bin/external/flatbuffers+/flatc",
                    f"bazel-out/{ex}/bin/external/flatbuffers+/src/_objs/flatc/flatc.o",
                ],
                "inputDepSetIds": [2],
                "outputIds": [flatc],
                "primaryOutputId": flatc,
            },
            {
                "targetId": 2,
                "configurationId": 2,
                "mnemonic": "Genrule",
                "arguments": [
                    "/bin/bash",
                    "-c",
                    (
                        f"source x.sh; bazel-out/{ex}/bin/external/flatbuffers+/flatc -c "
                        f"-o bazel-out/{tg}/bin monster.fbs"
                    ),
                ],
                "inputDepSetIds": [3],
                "outputIds": [hdr],
                "primaryOutputId": hdr,
                "executionPlatform": "@@platforms//host:host",
            },
            {
                "targetId": 2,
                "configurationId": 2,
                "mnemonic": "SymlinkTree",
                "arguments": [],
                "outputIds": [link],
            },
        ],
    }


def _records() -> dict[str, dict]:
    recs = naq.normalize(_graph(), "t", [("external/flatbuffers+", "$SRC")], "$MAIN", [])
    return {r["mnemonic"]: r for r in recs}


def test_normalize_config_roles_and_paths():
    r = _records()
    assert r["CppCompile"]["config_role"] == "exec"
    assert r["Genrule"]["config_role"] == "target"
    assert r["CppCompile"]["id"] == "$OUT_exec/bin/external/flatbuffers+/src/_objs/flatc/flatc.o"
    # --map rewrites the source tree, not the same component inside an output dir.
    assert r["CppCompile"]["inputs"]["explicit"] == ["$SRC/src/flatc.cpp"]
    assert r["CppLink"]["link_type"] == "executable"
    assert r["Genrule"]["target"] == "//:monster_fbs_srcs"
    assert naq.normalize_label("@@flatbuffers+//src:flatc") == "@flatbuffers//src:flatc"


def test_normalize_genrule_runs_exec_tool():
    g = _records()["Genrule"]
    main = g["steps"][g["main"]]
    assert main["argv"][0] == "$OUT_exec/bin/external/flatbuffers+/flatc"
    assert g["tool_built"] and g["tool_config"] == "exec"
    assert "$MAIN/monster.fbs" in main["argv"]
    assert g["execution_platform"] == "@platforms//host:host"
    assert g["kind"] == "custom" and g["target_rule"] == "genrule"


def test_normalize_housekeeping_and_exclusions():
    assert _records()["SymlinkTree"]["excluded"].startswith("bazel:")
    recs = naq.normalize(_graph(), None, [], None, [(r"^@flatbuffers//:flatc$", "tool")])
    assert {r["mnemonic"] for r in recs if r.get("excluded") == "tool"} == {
        "CppCompile",
        "CppLink",
    }


def test_config_roles_split_by_platform():
    g = naq.Graph(
        {
            "configuration": [
                {"id": 1, "mnemonic": "a", "platformName": "darwin_arm64"},
                {"id": 2, "mnemonic": "b", "platformName": "darwin_x86_64"},
                {"id": 3, "mnemonic": "c", "isTool": True},
            ]
        }
    )
    assert naq.config_roles(g) == {
        1: "target:darwin_arm64",
        2: "target:darwin_x86_64",
        3: "exec",
    }


def _bl(id_: str, kind: str, argv: list[str]) -> dict:
    return {"id": id_, "kind": kind, "steps": [{"argv": argv}], "main": 0}


def test_compare_shared_configuration():
    """In opt, buildlog's one flatc.o serves Bazel's target and exec copies."""
    bazel = [
        {**_bl("a", "compile", ["cc", "-O2", "-c", "$SRC/src/util.cpp"]), "config_role": r}
        for r in ("target", "exec")
    ]
    buildlog = [
        _bl(
            "$BUILD/obj/arm64-macos.release.none/f/u.o",
            "compile",
            ["c++", "-O3", "-c", "$SRC/src/util.cpp"],
        )
    ]
    out = cmp.compare(bazel, buildlog, {EXEC: ["target", "exec"]}, 10)
    assert "| exec | compile | 1 | 1 | 1 | 0 | 0 |" in out
    assert "| target | compile | 1 | 1 | 1 | 0 | 0 |" in out
    assert "one instance where Bazel has two): 1" in out
    assert "only bazel: -O2[1]" in out and "only buildlog: -O3[1]" in out


def test_compare_flags_drop_paths():
    f, inc = cmp.flags(["cc", "-Ia", "-iquote", "b", "-DX", "-c", "s.cpp", "-o", "s.o", "-MD"])
    assert f == {"-DX": 1} and inc == 2


# --- the checked-in baseline ---


def test_baseline_is_path_clean():
    for p in BASELINE_DIR.iterdir():
        if p.suffix in (".jsonl", ".txt"):
            text = p.read_text()
            assert "/Users/" not in text and "/home/" not in text, p.name


def test_baseline_flatc_in_exec_configuration():
    for v in VARIANTS:
        recs = [json.loads(line) for line in (BASELINE_DIR / f"bazel-{v}.jsonl").open()]
        gen = [r for r in recs if r["mnemonic"] == "Genrule" and "excluded" not in r]
        assert len(gen) == 1 and gen[0]["tool_config"] == "exec", v
        flatc = [r for r in recs if r["mnemonic"] == "CppLink" and r["id"].endswith("/flatc")]
        assert [r["config_role"] for r in flatc] == ["exec"], v
        cross = (BASELINE_DIR / f"compare-{v}.txt").read_text()
        assert "tool built in: Bazel exec, buildlog exec" in cross or (
            v == "opt" and "tool built in: Bazel exec, buildlog target+exec" in cross
        ), v


def test_project_has_no_exec_annotation():
    """The author names flatc as the program of an action; nothing says tool,
    exec or host (the check rule reads host_platform, it does not set it)."""
    spec = (PROJECT / "spec.toml").read_text()
    assert not re.search(r"\b(exec|host|tool_dep|cfg)\b", spec)
    heads = [
        line
        for line in (PROJECT / "rules.dl").read_text().splitlines()
        if not line.startswith(("//", " ")) and ":-" in line
    ]
    assert not [h for h in heads if re.match(r"(tool_dep|exec_config|host_platform|config)\(", h)]


# --- integration: configure and build the project ---


def _configure(project: Path, build: Path, config: str) -> dict:
    _, metrics = configure_project(
        project,
        build,
        user_files=[PRESET],
        configs=[config],
        var_overrides={"FLATBUFFERS_SRC": str(SRC)},
        keep_going=True,
    )
    return metrics


def _needs(fn, marks=needs_flatbuffers):
    for m in marks:
        fn = m(fn)
    return fn


def _file(p: Path) -> str:
    return subprocess.run(["file", "-b", str(p)], capture_output=True, text=True, check=True).stdout


@_needs
def test_native_build_runs_flatc_from_exec(tmp_path):
    """The debug target's genrule runs the release (exec) flatc; the header is Bazel's."""
    assert _configure(PROJECT, tmp_path, "native")["errors"] == []
    out = tmp_path / "souffle"
    subprocess.run(["ninja", "-C", str(out), "-j2"], check=True, capture_output=True)
    assert sorted(p.parent.name for p in (out / "bin").glob("*/flatc")) == [EXEC]
    assert (out / "bin" / f"{HOST_PLATFORM}.debug.none" / "sample_binary").exists()
    hdr = out / "gen" / f"{HOST_PLATFORM}.debug.none" / "monster_fbs" / "monster_generated.h"
    assert hashlib.sha256(hdr.read_bytes()).hexdigest() == HEADER_SHA


@_needs
@host.macos_only("the cross variant builds x86_64-macos")
def test_cross_build_runs_flatc_from_exec(tmp_path):
    metrics = _configure(PROJECT, tmp_path, "cross")
    assert metrics["errors"] == []
    out = tmp_path / "souffle"
    subprocess.run(["ninja", "-C", str(out), "-j2"], check=True, capture_output=True)
    # flatc exists once, in the exec configuration, and runs natively.
    assert sorted(p.parent.name for p in (out / "bin").glob("*/flatc")) == [EXEC]
    assert "arm64" in _file(out / "bin" / EXEC / "flatc")
    assert "x86_64" in _file(out / "bin" / "x86_64-macos.debug.none" / "sample_binary")
    hdr = out / "gen" / "x86_64-macos.debug.none" / "monster_fbs" / "monster_generated.h"
    assert hashlib.sha256(hdr.read_bytes()).hexdigest() == HEADER_SHA


@_needs
def test_misannotated_rule_is_caught(tmp_path, capsys):
    """The Bazel `srcs`-instead-of-`tools` mistake, in buildlog: the rule spells
    flatc's path in the product's configuration. Core's check rejects it."""
    proj = tmp_path / "proj"
    shutil.copytree(PROJECT, proj, ignore=shutil.ignore_patterns("buildlog.lock"))
    # [generator.X] always names its program by reference (argv[0] is a tool,
    # built for the build machine), so the mistake needs a hand-written rule,
    # as in Bazel: drop the generate row and spell flatc's path in the
    # product's configuration.
    spec = proj / "spec.toml"
    row = 'generate = [["fbs_cc", "monster_fbs", "samples/monster.fbs", "monster_generated.h"]]\n'
    assert row in spec.read_text()
    spec.write_text(spec.read_text().replace(row, ""))
    rules = proj / "rules.dl"
    rules.write_text(
        rules.read_text()
        + """
dep("monster_fbs", "flatc", "private").
action(o, "FLATC"), target_action("monster_fbs", c, o), default_output(o),
action_arg(o, 0, "", p), action_in(o, p, "implicit"), action_arg(o, 1, "", "-o"),
action_arg(o, 2, "", d), action_arg(o, 3, "", "src:samples/monster.fbs"),
action_in(o, "src:samples/monster.fbs", "explicit"),
iface("monster_fbs", c, "cc.generated_header", o) :-
  needed("monster_fbs", c), gen_dir("monster_fbs", c, d), o = cat(d, "/monster_generated.h"),
  executable("flatc", c, p).
"""
    )
    if host.MACOS:  # the mistake needs a product for another platform: cross has none on Linux
        # Core's exec check fails the generating action,
        # not the configuration: with keep_going the rest of the graph is emitted.
        metrics = _configure(proj, tmp_path / "b", "cross")
        err = capsys.readouterr().err
        assert "action runs a program built for another platform" in err
        assert "out:bin/x86_64-macos.debug.none/flatc" in err
        assert "the build machine is arm64-macos" in err
        assert any("another platform" in e for e in map(str, metrics["errors"]))
        ninja = (tmp_path / "b" / "souffle" / "build.ninja").read_text()
        failing = [
            ln for ln in ninja.splitlines() if ln.startswith("build ") and ln.endswith(": fail")
        ]
        assert any("monster_generated.h" in ln for ln in failing)
    # Native: the product's configuration is the host's, so the same rule passes.
    assert _configure(proj, tmp_path / "n", "native")["errors"] == []
