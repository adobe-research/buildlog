# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`why` cleanup: structural leaves, existence versus attributes, raw focus, origins."""

import json
from pathlib import Path

import pytest

from buildlog.cli import main
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
CROSS = ROOT / "examples" / "cross"
SIMD_OBJ = "obj/x86_64-macos.release.none/app/app/main.cpp.o"

needs_souffle = pytest.mark.skipif(
    not souffle_path().exists(), reason="patched Souffle not installed"
)


def run(capsys, *argv) -> str:
    capsys.readouterr()
    assert main([str(a) for a in argv]) == 0
    return capsys.readouterr().out


def why_json(capsys, build, *argv) -> dict:
    return json.loads(run(capsys, "why", "-C", build, "--format", "json", *argv, CROSS))


@pytest.mark.integration
@needs_souffle
def test_raw_leaves_prints_leaves_not_the_tree(cross_build, capsys):
    args = ["--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ, "--raw", "--leaves"]
    doc = why_json(capsys, cross_build, *args)
    root = doc["nodes"][doc["roots"][0]]
    assert root["children"] and all(doc["nodes"][c]["kind"] == "fact" for c in root["children"])
    text = run(capsys, "why", "-C", cross_build, *args, CROSS)
    assert not any(ln.startswith("  ") and "rule" in ln for ln in text.splitlines())
    assert len(text.splitlines()) < 80


def leaf_relations(doc: dict) -> set[str]:
    return {
        doc["relations"][doc["nodes"][c]["relation"]]["name"]
        for r in doc["roots"]
        for c in doc["nodes"][r]["children"]
    }


@pytest.mark.integration
@needs_souffle
def test_structural_leaves_hidden_by_default(cross_build, capsys):
    args = ["--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ, "--leaves"]
    default = leaf_relations(why_json(capsys, cross_build, *args))
    # The registries sit under configuration construction, which even the
    # focused views elide (the witness is the engine's choice): --full.
    shown = leaf_relations(why_json(capsys, cross_build, *args, "--structural", "--full"))
    raw = leaf_relations(why_json(capsys, cross_build, *args, "--raw"))
    core_bookkeeping = {"nkeys", "setting_key", "setting_value", "setting_default"}
    assert not default & core_bookkeeping
    assert {"exec_settings", "settings_name"}.isdisjoint(default)
    assert core_bookkeeping <= shown and core_bookkeeping <= raw
    # Declarations that carry meaning stay: the author's, the user's and the probes'.
    assert {"prop", "dep_when"} <= default
    # A configuration's declaration is not structural: shown where its
    # construction is (--full), never hidden as bookkeeping.
    assert "config_decl" in shown
    assert any(r.startswith("res_") for r in default)


@pytest.mark.integration
@needs_souffle
def test_plugin_knowledge_is_not_structural(cross_build, capsys):
    doc = why_json(capsys, cross_build, "--flag", "-DNDEBUG", "--leaves", "--max-targets", "50")
    assert "family_setting_arg" in leaf_relations(doc)
    assert not leaf_relations(doc) & {"set_key", "lattice_rank", "cc_check_kind"}


@pytest.mark.integration
@needs_souffle
def test_driver_and_manifest_origins(cross_build, capsys):
    def origin(atom):
        doc = why_json(capsys, cross_build, "--raw", atom)
        (node,) = doc["nodes"].values()
        (o,) = node["origins"]
        return o

    o = origin('package_provider("cps")')
    manifest = ROOT / "plugins" / "buildlog-cpp" / "src" / "buildlog_cpp" / "manifest.toml"
    lines = manifest.read_text().splitlines()
    assert o["kind"] == "manifest" and o["file"] == str(manifest)
    assert lines[o["line"] - 1] == 'name = "cps"'
    o = origin('target_qualifier("buildlog-cpp", "buildlog-cpp")')
    assert o["kind"] == "manifest" and lines[o["line"] - 1].startswith("name = ")
    o = origin('configure_arg(0, "env")')
    assert o["kind"] == "driver" and "configure command line" in o["detail"]
    text = run(capsys, "why", "-C", cross_build, 'package_provider("cps")', CROSS)
    assert "unknown" not in text and "manifest.toml:" in text


def leaf_texts(doc: dict) -> list[str]:
    return [doc["nodes"][c]["text"] for r in doc["roots"] for c in doc["nodes"][r]["children"]]


def test_shape_prunes_target_demand_only_beneath_action_attributes():
    from buildlog.provenance.shape import Shape, asked_targets, shape

    def doc(root_relation):
        fact = {"id": "n3", "kind": "fact", "relation": "request", "text": 'request("t", "c")',
                "origins": [{"kind": "spec", "file": "/p/spec.toml", "line": 4}]}  # fmt: skip
        nodes = {
            "n0": {"id": "n0", "kind": "derived", "relation": root_relation, "text": "root",
                   "rule": "r", "children": ["n1", "n4"]},
            "n1": {"id": "n1", "kind": "derived", "relation": "needed", "text": 'needed("t", "c")',
                   "tuple": ["t", "c"], "rule": "r", "children": ["n2", "n3"]},
            "n2": {"id": "n2", "kind": "derived", "relation": "dep_chain", "text": "dep_chain()",
                   "rule": "r", "children": ["n3", "n5"]},
            "n3": fact,
            "n4": {"id": "n4", "kind": "fact", "relation": "flag", "text": "flag(1)",
                   "origins": [{"kind": "spec", "file": "/p/spec.toml", "line": 9}]},
            "n5": {"id": "n5", "kind": "fact", "relation": "dep", "text": "dep()",
                   "origins": [{"kind": "spec", "file": "/p/spec.toml", "line": 5}]},
        }  # fmt: skip
        relations = {r: {"name": r} for r in ("dep_chain", "request", "flag", "dep")}
        relations[root_relation] = {"name": root_relation}
        relations["needed"] = {"name": "needed", "demand": "target"}
        return {"format": "buildlog-proof", "version": 1, "queries": ["root"], "roots": ["n0"],
                "nodes": nodes, "rules": {"r": {"relation": "x", "number": 1}},
                "relations": relations, "stats": {}}  # fmt: skip

    attribute = shape(doc("action_arg"), Shape())
    assert attribute["nodes"]["n1"]["demand_pruned"] is True
    assert not {"n2", "n3", "n5"} & set(attribute["nodes"])
    assert asked_targets(attribute) == ['needed("t", "c")']
    leaves = shape(doc("action_arg"), Shape(leaves=True))["nodes"]["n0"]["children"]
    assert sorted(leaves) == ["n1", "n4"]
    for existence in ("action", "action_out"):
        full = shape(doc(existence), Shape())
        assert "demand_pruned" not in full["nodes"]["n1"] and "n3" in full["nodes"]
    assert "demand_pruned" not in shape(doc("action_arg"), Shape(raw=True))["nodes"]["n1"]
    assert "demand_pruned" not in shape(doc("action_arg"), Shape(prune_demand=False))["nodes"]["n1"]


@pytest.mark.integration
@needs_souffle
def test_existence_is_pruned_beneath_a_flag_and_explained_by_demand(cross_build, capsys):
    args = ["--flag", "-DHAVE_SIMD=1", "--output", SIMD_OBJ]
    leaves = why_json(capsys, cross_build, *args, "--leaves")
    texts = leaf_texts(leaves)
    assert not any(t.startswith(("request(", "dep(")) for t in texts)
    needed = [n for n in leaves["nodes"].values() if n.get("relation", "").endswith("needed")]
    assert needed and all(n["demand_pruned"] for n in needed)
    text = run(capsys, "why", "-C", cross_build, *args, "--leaves", CROSS)
    assert "--demand explains why" in text and "built in this configuration" in text
    # The attribute's own reasons stay.
    assert any(t.startswith('prop("simd"') for t in texts)
    # Raw keeps the existence proof.
    raw = leaf_texts(why_json(capsys, cross_build, *args, "--leaves", "--raw"))
    assert any(t.startswith("request(") for t in raw)
    # --demand explains it: the request and dependency chain are back.
    demand = why_json(capsys, cross_build, *args, "--leaves", "--demand", "needed")
    assert all(demand["nodes"][r]["text"].startswith("needed(") for r in demand["roots"])
    assert any(t.startswith("request(") for t in leaf_texts(demand))
    assert demand["shape"]["demand"] == "needed"
    other = why_json(capsys, cross_build, *args, "--leaves", "--demand", "demand:target")
    assert other["roots"] == demand["roots"]


@pytest.mark.integration
@needs_souffle
def test_existence_queries_keep_the_existence_proof(cross_build, capsys):
    out = run(capsys, "why", "-C", cross_build, "--output", SIMD_OBJ, "--leaves", CROSS)
    assert 'request("app", "cross-release")' in out
    # The probes' requests are still pruned: each result cites its request,
    # so the request is not listed again, and nothing says why it was asked.
    assert "← probe inventory request" in out and "req_inventory(" not in out
    assert "built in this configuration" not in out


def test_engine_aggregate_origin_names_its_rule_and_unknown_says_why():
    from types import SimpleNamespace

    from buildlog.provenance.proof import Origins

    index = SimpleNamespace(
        probe_of_result={},
        packages={},
        inputs={"some_input"},
        clauses_by_head={},
        fact_clause=lambda rel, values: None,
        is_synthetic=lambda rel: rel.startswith("__agg"),
        display=lambda rel: (rel, None),
    )
    origins = Origins.__new__(Origins)
    origins.index, origins.by_key = index, {}
    rule = {"package": "buildlog", "file": "/x/L3.dl", "line": 9}
    (o,) = origins.of_fact("__agg_single", (1,), rule)
    assert o["kind"] == "aggregate" and o["file"] == "/x/L3.dl" and o["line"] == 9
    (o,) = origins.of_fact("some_input", ("a",), None)
    assert o["kind"] == "unknown" and "source-map" in o["detail"]
    (o,) = origins.of_fact("orphan", ("a",), None)
    assert o["kind"] == "unknown" and "no layer clause" in o["detail"]
