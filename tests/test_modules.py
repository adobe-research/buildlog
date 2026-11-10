# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dormant code is checked once; binding and use gate distinct model strata."""

import json

import pytest

from buildlog.engine import SouffleRounds
from buildlog.errors import SpecError
from buildlog.facts import project_fact_rows, read_rows, schema_from_rules, tuple_key, write_rows
from buildlog.layers import PackageRef, compose_program
from buildlog.paths import souffle_path

from .test_layers import write_layer

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle required")

METADATA = {
    "imported_kind": {("pkg::bootstrap", "static_library")},
    "import_linkage_dep": {("pkg", "host", "pkg::bootstrap", "static", "pkg::payload")},
    "package_version": {("pkg", "1")},
    "package_attr": {("pkg", "host", "feature", "yes")},
}


def kernel(tmp_path):
    return write_layer(
        tmp_path,
        "kernel",
        0,
        ".decl target(t:symbol)\n.decl effect(t:symbol)\n.decl unrelated(t:symbol)\n"
        ".decl defined(t:symbol)\ndefined(t) :- target(t).\n"
        ".decl bound(m:symbol,p:symbol)\n.decl used(m:symbol,p:symbol)\n"
        ".decl needed(t:symbol,p:symbol)\n"
        ".decl request(t:symbol,p:symbol)\n.decl split(t:symbol)\n"
        "split(t) :- request(t,_).\nneeded(t,p) :- request(t,p), !split(t).\n"
        ".decl imported_kind(t:symbol,k:symbol)\n"
        ".decl import_linkage_dep(n:symbol,p:symbol,t:symbol,l:symbol,d:symbol)\n"
        ".decl package_version(n:symbol,v:symbol)\n"
        ".decl package_attr(n:symbol,p:symbol,k:symbol,v:symbol)\n"
        ".decl module_bound(m:symbol,p:symbol)\n"
        ".decl module_used(m:symbol,p:symbol)\n"
        ".decl module_declared(m:symbol,t:symbol)\n"
        "module_bound(m,p) :- bound(m,p).\nmodule_used(m,p) :- used(m,p).\n"
        "module_used(m,p) :- module_declared(m,t), needed(t,p).\n",
        open=["target", "effect", *METADATA],
        exports=["needed"],
        accepts=["unrelated", "request"],
        driver=["bound", "used", "needed"],
    )


def test_declarations_payloads_and_actions_share_one_translated_program(tmp_path):
    root = kernel(tmp_path)
    escaped = 'sed "quoted" \\1'
    package = write_layer(
        tmp_path,
        "pkg",
        1,
        '#define DECLARE(T) target(cat("pkg::", T)), effect(T)\n'
        '.decl helper(t:symbol)\nhelper("bootstrap").\n'
        'DECLARE(t) :- helper(t).\neffect("fact").\n'
        'effect("split") :- helper("bootstrap"); helper("payload").\n'
        'imported_kind("pkg::bootstrap","static_library").\n'
        'import_linkage_dep("pkg","host","pkg::bootstrap","static","pkg::payload").\n'
        'package_version("pkg","1").\npackage_attr("pkg","host","feature","yes").\n'
        f"effect({json.dumps(escaped)}).\n",
        accepts=["helper"],
        extends=["target", "effect", *METADATA],
        requires={"kernel": ">=0.1"},
        facts=["params.toml"],
    )
    payload = package.parent / "params.toml"
    payload.write_text('helper = ["payload"]\n')
    destination = tmp_path / "program"
    rounds = SouffleRounds(destination / "build.dl")
    try:
        program = compose_program(
            [PackageRef(root, "core"), package],
            destination,
            tmp_path / "check",
            required_outputs=("target", "effect", *METADATA),
            required_inputs=("bound", "used", "needed"),
            explicit_manifests=frozenset({root}),
            composed_check=rounds.check_composed,
        )
        assert not program.errors, program.errors
        assert program.modules is not None
        schema = schema_from_rules([destination])
        schema.driver_inputs = set(program.driver_inputs)
        schema.module_facts = program.modules.facts
        project = tmp_path / "project"
        project.mkdir()
        (project / "buildlog.toml").write_text("spec = []\n")
        rows, origins, _ = project_fact_rows(project, schema, package_facts=[payload])
        staged = program.modules.facts[payload.resolve()]
        assert not rows["helper"]
        assert rows[staged.inputs["helper"]] == {("payload",)}
        assert str(payload) in origins[tuple_key(staged.inputs["helper"], ("payload",))][0]
        module = program.modules.identities[package.resolve()]
        rounds.set_outputs(program.outputs)
        processes = set()
        models = []
        metadata_models = []
        for index, (bound, used) in enumerate(((False, False), (True, False), (True, True))):
            facts = tmp_path / f"facts-{index}"
            facts.mkdir()
            for relation, values in rows.items():
                write_rows(facts / f"{relation}.facts", values)
            write_rows(facts / "bound.facts", [(module, "host")] if bound else [])
            write_rows(facts / "used.facts", [(module, "host")] if used else [])
            output = tmp_path / f"out-{index}"
            rounds.run(facts, output, frozenset({"target", "effect", *METADATA}))
            assert rounds.proc is not None
            processes.add(rounds.proc.pid)
            models.append(
                {name: set(read_rows(output / f"{name}.csv")) for name in ("target", "effect")}
            )
            metadata_models.append(
                {name: set(read_rows(output / f"{name}.csv")) for name in METADATA}
            )
        rounds.finish(output)
        assert len(processes) == 1
        assert models[0] == {"target": set(), "effect": set()}
        assert models[1] == {
            "target": {("pkg::bootstrap",), ("pkg::payload",)},
            "effect": set(),
        }
        assert models[2] == {
            "target": {("pkg::bootstrap",), ("pkg::payload",)},
            "effect": {("bootstrap",), ("payload",), ("fact",), ("split",), (escaped,)},
        }
        assert metadata_models == [{name: set() for name in METADATA}, METADATA, METADATA]
        payload.write_text('unrelated = ["unauthorized"]\n')
        with pytest.raises(SpecError, match="without declaring accepts or extends"):
            project_fact_rows(project, schema, package_facts=[payload])
    finally:
        rounds.close()


def test_invalid_executable_code_is_fatal_even_when_dormant(tmp_path):
    root = kernel(tmp_path)
    package = write_layer(
        tmp_path,
        "pkg",
        1,
        "effect(42).\n",
        extends=["effect"],
        requires={"kernel": ">=0.1"},
    )
    program = compose_program(
        [PackageRef(root, "core"), package],
        tmp_path / "program",
        tmp_path / "check",
        explicit_manifests=frozenset({root}),
        required_inputs=("bound", "used", "needed"),
    )
    assert program.build is None and program.errors
    assert any("composed program" in error for error in program.errors), program.errors


def test_declaration_helpers_must_not_depend_on_demand(tmp_path):
    root = kernel(tmp_path)
    package = write_layer(
        tmp_path,
        "pkg",
        1,
        '.decl helper(t:symbol)\nhelper(t) :- needed(t,_).\ntarget(cat("pkg::",t)) :- helper(t).\n',
        extends=["target"],
        requires={"kernel": ">=0.1"},
    )
    program = compose_program(
        [PackageRef(root, "core"), package],
        tmp_path / "program",
        tmp_path / "check",
        required_inputs=("bound", "used", "needed"),
        explicit_manifests=frozenset({root}),
    )
    assert program.build is None
    assert any(
        "declaration/selection stratum depends" in error and "helper" in error and "needed" in error
        for error in program.errors
    ), program.errors
