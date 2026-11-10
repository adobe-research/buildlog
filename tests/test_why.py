# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provenance: the proof protocol, shaping, `why`/`query`/`why-not` and the viewer.

Goldens live in tests/golden/. After an intended change to the layers or the
protocol, regenerate them with `BUILDLOG_UPDATE_GOLDEN=1 uv run pytest
tests/test_why.py` and review the diff.
"""

import json
import os
import re
import shlex
import shutil
from pathlib import Path

import pytest

from buildlog.cli import main
from buildlog.facts import _toml_value_lines
from buildlog.paths import souffle_path
from buildlog.provenance.shape import Shape, ShapeError, asked_requests, shape
from buildlog.provenance.values import (
    NIL,
    Adt,
    Record,
    explain_text,
    parse_atom,
    parse_constraint,
    parse_value,
    to_json,
)

from . import host
from .halide_env import SKIP_REASON, halide_available

ROOT = Path(__file__).resolve().parents[1]
CROSS = ROOT / "examples" / "cross"
GOLDEN = ROOT / "tests" / "golden"
HALIDE_PROJECTS = ROOT / "tests" / "projects" / "halide"
PACKAGE_PATH = ["--package-path", str(ROOT / "tests" / "fixtures" / "rule-packages")]
SIMD_OBJ = "obj/x86_64-macos.release.none/app/app/main.cpp.o"

needs_souffle = pytest.mark.skipif(
    not souffle_path().exists(), reason="patched Souffle not installed"
)


def run(capsys, *argv) -> str:
    capsys.readouterr()
    assert main([str(a) for a in argv]) == 0
    return capsys.readouterr().out


def spec_line(path: Path, needle: str) -> int:
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(f"{needle} not in {path}")


# --- values and source lines (no Soufflé) ----------------------------------


def test_values_parse_structured_labels():
    rel, values, neg = parse_atom(
        'use("app", ["x86_64-macos", [["native.opt", "release"], nil]], "cc.define", "A=\\"1\\"", 3)'
    )
    assert rel == "use" and not neg
    assert values[1] == Record(("x86_64-macos", Record((Record(("native.opt", "release")), NIL))))
    assert values[3] == 'A="1"' and values[4] == 3
    assert explain_text(values[3]) == '"A=\\"1\\""'
    _, (cond,), neg = parse_atom('!holds($Not($Eq("platform.cpu", "x86_64")))')
    assert neg and cond == Adt("Not", (Adt("Eq", ("platform.cpu", "x86_64")),))
    assert to_json(cond) == {
        "adt": "Not",
        "args": [{"adt": "Eq", "args": ["platform.cpu", "x86_64"]}],
    }
    assert parse_value("$None") == Adt("None")
    assert parse_value("[a, [b, nil]]", bare=True) == Record(("a", Record(("b", NIL))))
    assert parse_constraint("1.500000 < 2.000000") == ("<", 1.5, 2.0)
    assert parse_constraint('"a" != "b"') == ("!=", "a", "b")


def test_toml_value_lines_per_array_element():
    text = 'a = [\n  ["x", 1],\n  # c\n  ["y, ]", 2],\n]\nb = "z"\n\n[t]\nk = 1\n'
    lines = _toml_value_lines(text)
    assert lines["a"] == [1, 2, 4]
    assert lines["b"] == [6]
    assert "k" not in lines


# --- shaping on a synthetic document (no Soufflé) --------------------------


def _doc():
    """root <- mid (single premise) <- low <- {f1, f2}; root <- f3; f1 shared."""

    def fact(i, rel, line):
        return {
            "id": i,
            "kind": "fact",
            "relation": rel,
            "text": f"{rel}(1)",
            "origins": [{"kind": "spec", "file": "/p/spec.toml", "line": line}],
        }

    nodes = {
        "n0": {"id": "n0", "kind": "derived", "relation": "root", "text": "root(1)",
               "rule": "r0", "children": ["n1", "n5"]},
        "n1": {"id": "n1", "kind": "derived", "relation": "mid", "text": "mid(1)",
               "rule": "r1", "children": ["n2"]},
        "n2": {"id": "n2", "kind": "derived", "relation": "low", "text": "low(1)",
               "rule": "r2", "children": ["n3", "n4", "n6"]},
        "n3": fact("n3", "f1", 3),
        "n4": fact("n4", "f2", 4),
        "n5": fact("n5", "f3", 5),
        "n6": {"id": "n6", "kind": "derived", "relation": "+disconnected0",
               "text": "+disconnected0()", "rule": "r3", "children": ["n3"]},
    }  # fmt: skip
    relations = {r: {"name": r} for r in ("root", "mid", "low", "f1", "f2", "f3")}
    relations["+disconnected0"] = {"name": "+disconnected0", "synthetic": True}
    rules = {f"r{i}": {"relation": "x", "number": 1} for i in range(4)}
    return {"format": "buildlog-proof", "version": 1, "queries": ["root(1)"], "roots": ["n0"],
            "nodes": nodes, "rules": rules, "relations": relations, "stats": {}}  # fmt: skip


def test_shape_collapses_single_premise_chains():
    out = shape(_doc(), Shape())
    root = out["nodes"]["n0"]
    assert root["children"] == ["n2", "n5"]
    assert root["via"] == {"n2": ["n1"]}
    assert out["nodes"]["n1"]["collapsed"] is True
    # The synthetic node is spliced; its child is deduplicated.
    assert out["nodes"]["n2"]["children"] == ["n3", "n4"]
    assert "n6" not in out["nodes"]
    assert out["stats"]["shaped_nodes"] == 5
    uncollapsed = shape(_doc(), Shape(collapse=False))
    assert uncollapsed["nodes"]["n0"]["children"] == ["n1", "n5"]
    assert "via" not in uncollapsed["nodes"]["n0"]


def test_shape_depth_leaves_and_path_to():
    out = shape(_doc(), Shape(depth=1))
    assert out["nodes"]["n2"]["truncated"] and out["nodes"]["n2"]["hidden_children"] == 2
    assert "n3" not in out["nodes"]
    leaves = shape(_doc(), Shape(leaves=True))
    assert leaves["nodes"]["n0"]["children"] == ["n3", "n4", "n5"]
    path = shape(_doc(), Shape(path_to="spec.toml:4"))
    assert set(path["nodes"]) == {"n0", "n1", "n2", "n4"}
    assert path["nodes"]["n2"]["children"] == ["n4"]
    with pytest.raises(ShapeError):
        shape(_doc(), Shape(path_to="nothing-like-this"))


def test_shape_hides_structural_relations():
    doc = _doc()
    doc["relations"]["f3"]["structural"] = True
    assert shape(doc, Shape())["nodes"]["n0"]["children"] == ["n2"]
    assert shape(doc, Shape(hide_structural=False))["nodes"]["n0"]["children"] == ["n2", "n5"]


def test_shape_prunes_demand_beneath_the_answer():
    """mid is a probe's request: shown as one line, its subproof pruned."""
    doc = _doc()
    doc["relations"]["mid"]["demand"] = "kind"
    doc["nodes"]["n1"]["tuple"] = ["req-1"]
    doc["nodes"]["n5"]["origins"] = [{"kind": "probe", "probe": "other", "request": "req-2"}]
    # Folded (the default), the pruned request is dropped: n5 cites its probe.
    assert shape(doc, Shape())["nodes"]["n0"]["children"] == ["n5"]
    out = shape(doc, Shape(fold=False))
    assert out["nodes"]["n0"]["children"] == ["n1", "n5"]
    assert out["nodes"]["n1"]["demand_pruned"] is True
    assert "children" not in out["nodes"]["n1"] or out["nodes"]["n1"]["children"] == []
    assert not {"n2", "n3", "n4"} & set(out["nodes"])
    assert set(shape(doc, Shape(leaves=True))["nodes"]["n0"]["children"]) == {"n1", "n5"}
    assert set(shape(doc, Shape(path_to="demand:"))["nodes"]) == {"n0", "n1"}
    assert set(shape(doc, Shape(path_to="probe:kind"))["nodes"]) == {"n0", "n1"}
    assert asked_requests(out) == [("kind", "req-1"), ("other", "req-2")]
    # Unpruned on request, raw, or when the question is the demand itself.
    for full in (shape(doc, Shape(prune_demand=False)), shape(doc, Shape(raw=True))):
        assert {"n2", "n3", "n4"} <= set(full["nodes"])
        assert "demand_pruned" not in full["nodes"]["n1"]
    doc["roots"] = ["n1"]
    assert {"n2", "n3", "n4"} <= set(shape(doc, Shape())["nodes"])


# --- examples/cross (Soufflé) -----------------------------------------------


@pytest.fixture
def cross(cross_build):
    """examples/cross, configured once per worker (conftest.py)."""
    return cross_build


def _normalize(obj):
    text = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    return text.replace(str(ROOT), "<ROOT>") + "\n"


def _golden(name: str, doc: dict) -> None:
    doc = {k: v for k, v in doc.items() if k != "stats"}
    text = _normalize(doc)
    path = GOLDEN / name
    if os.environ.get("BUILDLOG_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert path.exists(), f"missing golden {path}; run with BUILDLOG_UPDATE_GOLDEN=1"
    assert text == path.read_text(), f"{name} differs from the golden"


def _why_json(capsys, build, *argv) -> dict:
    return json.loads(run(capsys, "why", "-C", build, "--format", "json", *argv, CROSS))


GOLDEN_CASES = {
    "cross-have-simd.json": ["--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ],
    "cross-have-simd-leaves.json": ["--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ, "--leaves"],
    "cross-lipo-depth2.json": [
        'action("out:bin/universal2-macos.release.none/app", "LIPO")',
        "--depth",
        "2",
    ],
}


@pytest.mark.integration
@needs_souffle
@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_proofs(cross, capsys, name):
    doc = _why_json(capsys, cross, *GOLDEN_CASES[name])
    assert doc["format"] == "buildlog-proof" and doc["version"] == 1
    for node in doc["nodes"].values():
        for c in node.get("children", []):
            assert c in doc["nodes"]
    _golden(name, doc)
    again = _why_json(capsys, cross, *GOLDEN_CASES[name])
    assert _normalize(again) == _normalize(doc)


@pytest.mark.integration
@needs_souffle
def test_leaf_origin_is_the_toml_line(cross, capsys):
    doc = _why_json(capsys, cross, "--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ, "--leaves",
                    "--with-demand")  # fmt: skip
    by_text = {n["text"]: n for n in doc["nodes"].values()}
    spec = CROSS / "spec.toml"
    for text, needle in [
        ('request("app", "cross-release")', '["app", "cross-release"]'),
        ('prop("simd", "cc.define", "HAVE_SIMD=1", "public")', '"HAVE_SIMD=1"'),
        ('config_decl("cross-release", "x86_64-macos")', '["cross-release", "x86_64-macos"]'),
    ]:
        (origin,) = by_text[text]["origins"]
        assert origin["kind"] == "spec"
        assert origin["file"] == str(spec)
        assert origin["line"] == spec_line(spec, needle), text
    out = run(capsys, "why", "-C", cross, 'request("app", "u2-release")', CROSS)
    assert f"examples/cross/spec.toml:{spec_line(spec, '["app", "u2-release"]')}" in out


@pytest.mark.integration
@needs_souffle
def test_probe_origin_leaves(cross, capsys):
    doc = _why_json(capsys, cross, "--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ, "--leaves")
    probes = [n for n in doc["nodes"].values() if n["relation"].startswith("res_")]
    assert {n["relation"] for n in probes} >= {"res_platform_constraint"}
    for n in probes:
        (origin,) = n["origins"]
        assert origin["kind"] == "probe"
        assert origin["probe"] == "inventory"
        assert origin["request"] == "inventory:default"
        assert origin["package"] == "buildlog-cpp"
        assert doc["relations"][n["relation"]]["probe"] == "inventory"
    text = run(capsys, "why", "-C", cross, "--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ,
               "--path-to", "probe:inventory", CROSS)  # fmt: skip
    assert "← probe inventory request" in text
    assert "spec.toml" not in text


@pytest.mark.integration
@needs_souffle
def test_importer_origin_objects_pass_through(cross, capsys, tmp_path):
    build = tmp_path / "cross"
    shutil.copytree(cross, build, symlinks=True)
    final = json.loads((build / "metrics.json").read_text())["final_round"]
    source_map = build / "facts-in" / f"round-{final}" / "source_map.json"
    data = json.loads(source_map.read_text())
    key = json.dumps(["request", ["app", "cross-release"]])
    assert key in data
    origin = {"kind": "pkg-config", "file": "/opt/x/lib/pkgconfig/x.pc", "line": 7, "package": "x"}
    data[key] = [origin]
    source_map.write_text(json.dumps(data))
    args = ["--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ, "--with-demand"]
    doc = _why_json(capsys, build, *args, "--leaves")
    (leaf,) = [n for n in doc["nodes"].values() if n["text"] == 'request("app", "cross-release")']
    assert leaf["origins"] == [origin]
    text = run(capsys, "why", "-C", build, *args, "--path-to", "package:x", CROSS)
    assert 'request("app", "cross-release")  ← pkg-config /opt/x/lib/pkgconfig/x.pc:7' in text
    assert "spec.toml" not in text


@pytest.mark.integration
@needs_souffle
def test_flag_and_output_map_to_action_tuples(cross, capsys):
    doc = _why_json(capsys, cross, "--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ, "--depth", "0")
    assert doc["queries"] == [f'action_arg("out:{SIMD_OBJ}", 21, "-D", "HAVE_SIMD=1")']
    root = doc["nodes"][doc["roots"][0]]
    assert root["tuple"] == [f"out:{SIMD_OBJ}", 21, "-D", "HAVE_SIMD=1"]
    # An absolute Ninja path works too; a path token matches `src:` values.
    abs_out = cross / "souffle" / SIMD_OBJ
    doc = _why_json(capsys, cross, "--flag", "-c app/main.cpp", "--output", abs_out, "--depth", "0")
    assert doc["queries"] == [f'action_arg("out:{SIMD_OBJ}", 41, "-c ", "src:app/main.cpp")']
    # --output alone explains the action itself.
    doc = _why_json(capsys, cross, "--output", "bin/universal2-macos.release.none/app",
                    "--depth", "0")  # fmt: skip
    assert doc["queries"] == ['action("out:bin/universal2-macos.release.none/app", "LIPO")']
    # --flag alone finds every command line with the token.
    doc = _why_json(capsys, cross, "--flag", "-DHAVE_SIMD=1", "--depth", "0")
    assert len(doc["queries"]) >= 1
    assert all("HAVE_SIMD=1" in q for q in doc["queries"])
    assert main(["why", "-C", str(cross), "--flag", "-DHAVE_SIMD=2", str(CROSS)]) == 1
    assert "did you mean -DHAVE_SIMD=1" in capsys.readouterr().err


@pytest.mark.integration
@needs_souffle
def test_depth_and_collapse(cross, capsys):
    args = ["--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ]
    full = _why_json(capsys, cross, *args)
    flat = _why_json(capsys, cross, *args, "--no-collapse")
    raw = _why_json(capsys, cross, *args, "--raw")
    assert full["stats"]["shaped_nodes"] < flat["stats"]["shaped_nodes"] <= len(raw["nodes"])
    assert any(n.get("via") for n in full["nodes"].values())
    assert not any(n.get("via") for n in flat["nodes"].values())
    assert any(n.get("relation") == "+disconnected0" or n["kind"] == "derived"
               for n in raw["nodes"].values())  # fmt: skip
    shallow = _why_json(capsys, cross, *args, "--depth", "1")
    root = shallow["nodes"][shallow["roots"][0]]
    child = shallow["nodes"][root["children"][0]]
    assert child.get("truncated") and child["hidden_children"] > 0
    text = run(capsys, "why", "-C", cross, *args, "--depth", "1", CROSS)
    assert "more" in text and len(text.splitlines()) <= 6


@pytest.mark.integration
@needs_souffle
def test_query_table_and_json(cross, capsys):
    out = run(capsys, "query", "-C", cross, 'link_file(t, c, f), t = "simd"', CROSS)
    lines = out.splitlines()
    assert lines[0].split() == ["t", "c", "f"]
    assert lines[2].split() == [
        "simd", "‹x86_64-macos.release.none›", "out:lib/x86_64-macos.release.none/libsimd.a"
    ]  # fmt: skip
    assert "(1 row)" in out
    assert '‹x86_64-macos.release.none› = ["x86_64-macos"' in out
    doc = json.loads(run(capsys, "query", "-C", cross, "--format", "json",
                         'action(a, "LIPO")', CROSS))  # fmt: skip
    assert doc["variables"] == [{"name": "a", "type": "symbol"}]
    assert doc["rows"] == [["out:bin/universal2-macos.release.none/app"]]
    doc = json.loads(run(capsys, "query", "-C", cross, "--format", "json",
                         'condition("is_x86", c)', CROSS))  # fmt: skip
    assert doc["rows"] == [[{"adt": "Eq", "args": ["platform.cpu", "x86_64"]}]]
    assert run(capsys, "query", "-C", cross, 'target("app")', CROSS) == "true.\n"
    assert run(capsys, "query", "-C", cross, 'target("nope")', CROSS) == "false.\n"


@pytest.mark.integration
@needs_souffle
def test_why_not_names_the_failing_literal(cross, capsys):
    fact = f'action_arg("out:{SIMD_OBJ}", 21, "-D", "HAVE_SIMD=0")'
    doc = json.loads(run(capsys, "why-not", "-C", cross, "--format", "json", fact, CROSS))
    assert doc["format"] == "buildlog-why-not" and not doc["holds"]
    # Five rules can derive it: L4's define rules for C++ and C sources,
    # core's (an argv position filled with the path of a tool the action
    # runs), which needs action_run, and core's template-piece rule of a
    # configured file, whose computed rank and prefix the head check cannot
    # rule out; the generator vocabulary's argv rule (no generators here);
    # and L4_modules' argv rules for the scan, BMI and std actions and for
    # module-aware objects (no module-aware target here).
    by_first = {r["literals"][0]["relation"]: r for r in doc["rules"]}
    assert set(by_first) == {
        "action_run",
        "cxx_object",
        "c_object",
        "configured_file",
        "gen_action",
        "cxx_scan_of",
        "cxx_bmi_action",
        "cxx_std_unit",
        "cxx_aware",
    }
    assert by_first["gen_action"]["literals"][0]["holds"] is False
    assert by_first["configured_file"]["literals"][0]["holds"] is False
    assert by_first["action_run"]["rule"]["id"].startswith("buildlog:L1_targets")
    # No C objects at all: the C rule's first literal fails outright.
    c_rule = by_first["c_object"]
    assert c_rule["first_failure"] == 0 and c_rule["literals"][0]["holds"] is False
    rule = by_first["cxx_object"]
    assert rule["rule"]["id"].startswith("buildlog-cpp:layers/L4_cxx")
    first, second = rule["literals"]
    assert first["relation"] == "cxx_object" and first["holds"] is True
    assert second["relation"] == "use" and second["holds"] is False
    assert rule["first_failure"] == 1
    text = run(capsys, "why-not", "-C", cross, fact, CROSS)
    assert "✗ use(" in text and "first failing literal" in text
    held = run(capsys, "why-not", "-C", cross, fact.replace("=0", "=1"), CROSS)
    assert "holds" in held


@pytest.mark.integration
@needs_souffle
def test_html_viewer(cross, capsys, tmp_path):
    out = tmp_path / "why.html"
    run(capsys, "why", "-C", cross, "--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ,
        "--full", "--html", out, CROSS)  # fmt: skip
    page = out.read_text()
    assert page.startswith("<!DOCTYPE html>")
    assert "<details open>" in page
    assert "action_arg(&quot;out:" in page
    line = spec_line(CROSS / "spec.toml", '["cross-release", "x86_64-macos"]')
    assert f'href="{(CROSS / "spec.toml").as_uri()}#L{line}"' in page
    assert not re.search(r'(src|href)="https?:', page)
    embedded = re.search(
        r'<script type="application/json" id="proof">(.*?)</script>', page, re.DOTALL
    )
    assert (
        embedded
        and json.loads(embedded.group(1).replace("<\\/", "</"))["format"] == "buildlog-proof"
    )
    assert '<span class="demand">[asked: probe inventory request' in page
    asked = tmp_path / "why-demand.html"
    run(capsys, "why", "-C", cross, "--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ,
        "--demand", "*", "--html", asked, CROSS)  # fmt: skip
    page = asked.read_text()
    assert "buildlog why --demand" in page and "req_inventory(" in page
    assert 'class="demand"' not in page


@host.cross
@pytest.mark.integration
@needs_souffle
def test_why_symbol_with_quote_and_backslash(tmp_path, capsys):
    project = tmp_path / "cross"
    shutil.copytree(CROSS, project, ignore=shutil.ignore_patterns("build"))
    spec = project / "spec.toml"
    spec.write_text(spec.read_text().replace(
        "prop = [\n", 'prop = [\n  ["simd", "cc.define", "MSG=\\"a\\\\b\\"", "public"],\n', 1
    ))  # fmt: skip
    build = tmp_path / "build"
    assert (
        main(
            [
                "configure",
                "--user",
                str(project / "presets" / "apple-clang.toml"),
                "-C",
                str(build),
                str(project),
            ]
        )
        == 0
    )
    # Fact files are written raw and Soufflé reads them verbatim, so the value
    # reaches the model, and the compile line, unchanged.
    held = r'MSG="a\b"'
    obj = "obj/x86_64-macos.release.none/simd/simd/simd.cpp.o"
    ninja = (build / "souffle" / "build.ninja").read_text().splitlines()
    (cmd,) = [ninja[i + 2] for i, ln in enumerate(ninja) if ln.startswith(f"build {obj}:")]
    assert f"-D{held}" in shlex.split(cmd.removeprefix("  cmd = "))
    doc = json.loads(run(capsys, "why", "-C", build, "--format", "json", "--leaves",
                         "--flag", f"-D{held}", "--output", obj, project))  # fmt: skip
    root = doc["nodes"][doc["roots"][0]]
    assert root["tuple"] == [f"out:{obj}", 20, "-D", held]
    (leaf,) = [n for n in doc["nodes"].values() if n.get("relation") == "prop"
               and n["tuple"][2] == held]  # fmt: skip
    (origin,) = leaf["origins"]
    assert origin["file"] == str(spec) and origin["line"] == spec_line(spec, "MSG=")
    # The tuple text round-trips through `explain`.
    text = run(capsys, "why", "-C", build, "--depth", "0", leaf["text"], project)
    assert text.startswith(leaf["text"])
    q = json.loads(run(capsys, "query", "-C", build, "--format", "json",
                       'prop("simd", "cc.define", v, "public")', project))  # fmt: skip
    assert [held] in q["rows"]


# --- Halide bilateral_grid ------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(not halide_available(), reason=SKIP_REASON)
def test_halide_why_flag(bilateral_grid_build, capsys):
    project = HALIDE_PROJECTS / "bilateral_grid"
    build = bilateral_grid_build
    out = run(capsys, "why", *PACKAGE_PATH, "-C", build, "--flag", "-DHALIDE_WITH_EXCEPTIONS",
              "--leaves", project)  # fmt: skip
    facts = ROOT / "tests/fixtures/rule-packages/share/buildlog/halide/halide.facts.toml"
    line = spec_line(facts, '"HALIDE_WITH_EXCEPTIONS"')
    assert (
        'prop("Halide::Halide", "cc.define", "HALIDE_WITH_EXCEPTIONS", "interface")  '
        f"← tests/fixtures/rule-packages/share/buildlog/halide/halide.facts.toml:{line} "
        "(package halide)"
    ) in out
    # A define holds on every platform: the platform facts under the
    # configuration's construction are not reasons, and not listed.
    assert "← probe inventory" not in out
    doc = json.loads(run(capsys, "why", *PACKAGE_PATH, "-C", build, "--format", "json",
                         "--flag", "-DHALIDE_WITH_EXCEPTIONS", project))  # fmt: skip
    assert doc["stats"]["raw_bytes"] > 50_000
    assert doc["stats"]["shaped_nodes"] < doc["stats"]["dag_nodes"] < doc["stats"]["raw_tree_nodes"]


# --- T16: folded view, names in goals, targets and loose selectors ----------


@pytest.mark.integration
@needs_souffle
def test_why_target_by_name_and_config(cross, capsys):
    """`why app --config cross-release`: the demand chain, folded to the
    user's request and declaration; --full shows the construction."""
    out = run(capsys, "why", "-C", cross, "app", "--config", "cross-release", CROSS)
    lines = out.splitlines()
    assert lines[0].startswith('needed("app", ‹x86_64-macos.release.none›)')
    assert any('request("app", "cross-release")' in line for line in lines)
    assert "folded]" in out and "decl_suffix" not in out and "legend:" not in out
    assert " > " not in out and "+disconnected" not in out
    assert len(lines) <= 8
    full = run(capsys, "why", "-C", cross, "app", "--config", "cross-release", "--full", CROSS)
    assert "decl_suffix" in full and "legend:" in full
    # By printed name, a prefix of it, or the declaration; in goals too.
    for name in ("x86_64-macos.release.none", "x86_64-macos", "cross-release"):
        assert run(capsys, "why", "-C", cross, "app", "--config", name, CROSS) == out
    for goal in (
        'needed("app", @x86_64-macos.release.none)',
        'needed("app", ‹x86_64-macos.release.none›)',
        'needed("app", @cross-release)',
    ):
        assert run(capsys, "why", "-C", cross, goal, CROSS) == out


@host.cross
@pytest.mark.integration
@needs_souffle
def test_why_names_errors(cross, capsys):
    capsys.readouterr()
    assert main(["why", "-C", str(cross), "app", "--config", "nope", str(CROSS)]) == 1
    assert "--config nope names no configuration" in capsys.readouterr().err
    assert main(["why", "-C", str(cross), 'needed("app", @arm64-macos)', str(CROSS)]) == 1
    assert "@arm64-macos is ambiguous" in capsys.readouterr().err
    # A quoted string is never a name.
    out = run(capsys, "query", "-C", cross, 'config_decl(d, p), d = "@cross-release"', CROSS)
    assert "(0 rows)" in out


@pytest.mark.integration
@needs_souffle
def test_why_loose_output_and_flag(cross, capsys):
    exact = _why_json(capsys, cross, "--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ)
    # A source file and a configuration instead of the object path.
    loose = _why_json(
        capsys, cross, "--flag", "-DHAVE_SIMD=1", "--output", "app/main.cpp",
        "--config", "cross-release",
    )  # fmt: skip
    assert loose["roots"] == exact["roots"]
    # A prefix of the token.
    prefix = _why_json(capsys, cross, "--flag", "-DHAVE_SIMD", "--output", SIMD_OBJ)
    assert [prefix["nodes"][r]["text"] for r in prefix["roots"]] == [
        exact["nodes"][r]["text"] for r in exact["roots"]
    ]
    # A target and the basename of the compiler: its link action, once.
    doc = _why_json(capsys, cross, "app", "--flag", "clang++", "--config", "cross-release")
    (root,) = doc["roots"]
    text = doc["nodes"][root]["text"]
    assert text.startswith('action_arg("out:bin/x86_64-macos.release.none/app", 0, ""')
    assert text.endswith('/clang++")')


def test_error_order_puts_causes_first():
    from buildlog.engine import error_order

    rows = [
        ("action input is neither a source nor produced", "x"),
        ("unresolved dependency", "dependency z of app has no definition"),
        ("unknown condition", "is_x68"),
    ]
    assert [k for k, _ in sorted(rows, key=error_order)] == [
        "unknown condition",
        "unresolved dependency",
        "action input is neither a source nor produced",
    ]


@pytest.mark.integration
@needs_souffle
def test_lattice_join_cites_the_winning_candidate(tmp_path, capsys):
    host.require_macos()
    src = tmp_path / "cross"
    shutil.copytree(CROSS, src, ignore=shutil.ignore_patterns("build", "build-*"))
    spec = src / "spec.toml"
    winner = '["util", "cxx.std", "20", "public"],'
    text = spec.read_text()
    assert text.count(winner) == 1
    spec.write_text(text.replace(winner, winner + '\n  ["app", "cxx.std", "17", "private"],'))
    build = tmp_path / "build"
    user = src / "presets" / "apple-clang.toml"
    run(capsys, "configure", "--user", user, "-C", build, src)
    out = run(capsys, "why", "-C", build, "--leaves", "--flag", "-std=c++20", "--output", SIMD_OBJ,
              src)  # fmt: skip
    assert f"spec.toml:{spec_line(spec, winner)}" in out
    assert f"spec.toml:{spec_line(spec, '["app", "cxx.std", "17"')}" not in out
