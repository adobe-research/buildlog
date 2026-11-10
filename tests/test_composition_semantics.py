# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Executable design gate, not the production package selector.

The two policies share selection and query rules. Only the lower definition
scope differs; neither policy derives definitions from query-time demand.
"""

from __future__ import annotations

import subprocess

import pytest

from buildlog.facts import read_rows, write_rows
from buildlog.paths import souffle_path

pytestmark = pytest.mark.skipif(not souffle_path().is_file(), reason="patched Souffle required")

INPUTS = {
    "policy": "value:symbol",
    "av_offer": "name:symbol, platform:symbol, bundle:symbol, kind:symbol",
    "av_decl": "bundle:symbol, name:symbol, target:symbol",
    "av_alias": "bundle:symbol, name:symbol, target:symbol",
    "av_include": "bundle:symbol, child:symbol",
    "av_same": "bundle:symbol, canonical:symbol",
    "av_bad": "bundle:symbol",
    "av_dep": "bundle:symbol, owner:symbol, dep:symbol",
    "av_optional": "bundle:symbol, owner:symbol, dep:symbol",
    "av_file": "bundle:symbol, file:symbol",
    "av_module": "bundle:symbol, module:symbol",
    "av_module_decl": "module:symbol, name:symbol, target:symbol",
    "av_module_flag": "module:symbol, owner:symbol, flag:symbol",
    "pin": "name:symbol, platform:symbol, bundle:symbol",
    "explicit_file": "bundle:symbol, platform:symbol",
    "scope_root": "name:symbol, platform:symbol",
    "source": "name:symbol, target:symbol",
    "source_alias": "name:symbol, target:symbol",
    "request": "name:symbol, platform:symbol",
    "res_arch": "file:symbol, platform:symbol, ok:symbol",
}
OUTPUTS = ("defined", "select", "need", "use_bundle", "flag", "error", "req_arch")

RULES = r"""
.decl includes(root:symbol, child:symbol)
includes(b,b) :- av_offer(_,_,b,_).
includes(b,b) :- explicit_file(b,_).
includes(b,b) :- pin(_,_,b).
includes(b,c) :- includes(b,x), av_include(x,c).

.decl pin_bundle(platform:symbol, bundle:symbol)
pin_bundle(p,c) :- pin(_,p,b), includes(b,c).
.decl effective_pin(name:symbol, platform:symbol, bundle:symbol)
effective_pin(n,p,b) :- pin(n,p,b).
effective_pin(n,p,b) :- pin_bundle(p,b), av_decl(b,n,_).
.decl pinned(name:symbol, platform:symbol)
pinned(n,p) :- effective_pin(n,p,_).
.decl duplicate(name:symbol, platform:symbol, bundle:symbol)
duplicate(n,p,b) :- av_offer(n,p,b,_), av_same(b,c), av_offer(n,p,c,_).
.decl canonical(name:symbol, platform:symbol)
canonical(n,p) :- av_offer(n,p,b,"defining"), !duplicate(n,p,b).
.decl source_bound(name:symbol)
source_bound(n) :- source(n,_).
source_bound(n) :- source_alias(n,_).
.decl candidate(name:symbol, platform:symbol, bundle:symbol)
candidate(n,p,b) :- av_offer(n,p,b,"defining"), !duplicate(n,p,b), !source_bound(n).
candidate(n,p,b) :- av_offer(n,p,b,"alias"), !canonical(n,p),
  !duplicate(n,p,b), !source_bound(n).
candidate(n,p,b) :- av_offer(n,p,b,_), effective_pin(n,p,b), !source_bound(n).
.decl ambiguous(name:symbol, platform:symbol)
ambiguous(n,p) :- candidate(n,p,a), candidate(n,p,b), a != b.
.decl select(name:symbol, platform:symbol, bundle:symbol)
select(n,p,b) :- candidate(n,p,b), !pinned(n,p), !ambiguous(n,p).
select(n,p,b) :- candidate(n,p,b), effective_pin(n,p,b).

.decl declaration_bundle(bundle:symbol, platform:symbol)
declaration_bundle(b,p) :- select(_,p,r), includes(r,b).
declaration_bundle(b,p) :- candidate(n,p,r), ambiguous(n,p), !pinned(n,p), includes(r,b).
declaration_bundle(b,p) :- explicit_file(r,p), includes(r,b).
.decl module_bound(module:symbol, platform:symbol)
module_bound(m,p) :- declaration_bundle(b,p), av_module(b,m).
.decl bound_bundle(bundle:symbol, platform:symbol)
bound_bundle(b,p) :- select(_,p,r), includes(r,b).
bound_bundle(b,p) :- explicit_file(r,p), includes(r,b).
.decl resolution(name:symbol, platform:symbol, bundle:symbol, target:symbol)
resolution(n,p,b,t) :- bound_bundle(b,p), av_decl(b,n,t).
resolution(n,p,b,t) :- bound_bundle(x,p), av_alias(x,n,a), resolution(a,p,b,t).
resolution(n,p,b,t) :- source_alias(n,a), resolution(a,p,b,t).
resolution(n,p,b,t) :- module_bound(m,p), av_module(b,m), av_module_decl(m,n,t).

.decl scope(name:symbol, platform:symbol)
scope(n,p) :- scope_root(n,p).
scope(a,p) :- scope(n,p), source_alias(n,a).
scope(d,p) :- scope(n,p), resolution(n,p,b,_), av_dep(b,n,d).
.decl visible_bundle(bundle:symbol, platform:symbol)
visible_bundle(b,p) :- policy("available"), declaration_bundle(b,p).
visible_bundle(b,p) :- policy("scoped"), scope(n,p), select(n,p,r), includes(r,b).
visible_bundle(b,p) :- policy("scoped"), scope(n,p), candidate(n,p,r),
  ambiguous(n,p), !pinned(n,p), includes(r,b).
visible_bundle(b,p) :- explicit_file(r,p), includes(r,b).
.decl defined(name:symbol)
defined(n) :- source(n,_).
defined(n) :- source_alias(n,a), defined(a).
defined(n) :- visible_bundle(b,_), av_decl(b,n,_), !av_bad(b).
defined(n) :- visible_bundle(b,_), av_alias(b,n,a), !av_bad(b), defined(a).
defined(n) :- visible_bundle(b,_), av_module(b,m), av_module_decl(m,n,_).

.decl need(name:symbol, platform:symbol)
need(n,p) :- request(n,p).
need(a,p) :- need(n,p), source_alias(n,a).
need(d,p) :- need(n,p), resolution(n,p,b,_), av_dep(b,n,d).
need(d,p) :- need(n,p), resolution(n,p,b,_), av_optional(b,n,d), defined(d).
.decl use_bundle(bundle:symbol, platform:symbol)
use_bundle(r,p) :- need(n,p), select(n,p,r).
use_bundle(b,p) :- use_bundle(r,p), includes(r,b).
use_bundle(b,p) :- need(n,p), resolution(n,p,b,_).
use_bundle(b,p) :- explicit_file(b,p).
.decl module_used(module:symbol, platform:symbol)
module_used(m,p) :- use_bundle(b,p), av_module(b,m).
.decl flag(owner:symbol, platform:symbol, value:symbol)
flag(n,p,cat("NO_",d)) :- need(n,p), resolution(n,p,b,_), av_optional(b,n,d), !defined(d).
flag(n,p,f) :- module_used(m,p), need(n,p), av_module_flag(m,n,f).

.decl req_arch(file:symbol, platform:symbol)
req_arch(f,p) :- use_bundle(b,p), av_file(b,f).
.decl error(kind:symbol, name:symbol, platform:symbol)
error("ambiguous",n,p) :- need(n,p), ambiguous(n,p), !pinned(n,p).
error("two pins",n,p) :- need(n,p), effective_pin(n,p,a), effective_pin(n,p,b), a != b.
error("pin not a candidate",n,p) :- need(n,p), effective_pin(n,p,b), !candidate(n,p,b).
error("malformed metadata",b,p) :- use_bundle(b,p), av_bad(b).
.decl can_resolve(name:symbol, platform:symbol)
can_resolve(n,p) :- resolution(n,p,_,_).
can_resolve(n,p) :- source(n,_), request(_,p).
can_resolve(n,p) :- source_alias(n,a), can_resolve(a,p).
error("missing",n,p) :- need(n,p), !can_resolve(n,p), !ambiguous(n,p), !pinned(n,p).
error("wrong architecture",f,p) :- req_arch(f,p), res_arch(f,p,"no").
"""


def program(extra=""):
    return (
        "\n".join(f".decl {name}({attrs})\n.input {name}" for name, attrs in INPUTS.items())
        + RULES
        + extra
        + "\n"
        + "\n".join(f".output {name}" for name in OUTPUTS)
        + "\n"
    )


def materialize(directory, policy, rows):
    directory.mkdir()
    for relation in INPUTS:
        write_rows(
            directory / f"{relation}.facts",
            [(policy,)] if relation == "policy" else rows.get(relation, []),
        )


def evaluate(tmp_path, policy, rows, extra="", *, reject=False, diagnostic="stratif"):
    facts, out = tmp_path / "facts", tmp_path / "out"
    materialize(facts, policy, rows)
    out.mkdir()
    path = tmp_path / "model.dl"
    path.write_text(program(extra))
    result = subprocess.run(
        [str(souffle_path()), "-w", "-F", str(facts), "-D", str(out), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if reject:
        assert result.returncode, result.stdout + result.stderr
        assert diagnostic in result.stderr.lower(), result.stderr
        return result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    return {name: set(read_rows(out / f"{name}.csv")) for name in OUTPUTS}


def library(name, bundle=None, platform="host"):
    bundle = bundle or name
    return {
        "av_offer": [(name, platform, bundle, "defining")],
        "av_decl": [(bundle, name, name)],
    }


def combine(*groups):
    result = {}
    for group in groups:
        for relation, rows in group.items():
            result.setdefault(relation, []).extend(rows)
    return result


@pytest.mark.parametrize("policy", ["available", "scoped"])
@pytest.mark.parametrize("explicit", [False, True])
def test_optional_definition_and_negative_branch(tmp_path, policy, explicit):
    rows = combine(
        library("ImageIO"),
        library("PNG"),
        {
            "request": [("ImageIO", "host")],
            "scope_root": [("ImageIO", "host")] + ([("PNG", "host")] if explicit else []),
            "av_optional": [("ImageIO", "ImageIO", "PNG")],
        },
    )
    model = evaluate(tmp_path, policy, rows)
    enabled = policy == "available" or explicit
    assert (("PNG",) in model["defined"]) == enabled
    assert (("PNG", "host") in model["need"]) == enabled
    assert (("PNG", "host") in model["use_bundle"]) == enabled
    assert (("ImageIO", "host", "NO_PNG") in model["flag"]) != enabled
    assert not model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_unconditional_dependencies_expand_static_scope(tmp_path, policy):
    rows = combine(
        library("app"),
        library("dep"),
        library("leaf"),
        {
            "request": [("app", "host")],
            "scope_root": [("app", "host")],
            "av_dep": [("app", "app", "dep"), ("dep", "dep", "leaf")],
        },
    )
    model = evaluate(tmp_path, policy, rows)
    assert model["defined"] == {("app",), ("dep",), ("leaf",)}
    assert model["need"] == {("app", "host"), ("dep", "host"), ("leaf", "host")}
    assert not model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_optional_definitions_do_not_follow_sibling_query_demand(tmp_path, policy):
    rows = combine(
        library("ImageIO"),
        library("PNG"),
        library("JPEG"),
        {
            "request": [("ImageIO", "host"), ("JPEG", "host")],
            "scope_root": [("ImageIO", "host"), ("PNG", "host")],
            "av_optional": [("ImageIO", "ImageIO", "PNG"), ("ImageIO", "ImageIO", "JPEG")],
        },
    )
    model = evaluate(tmp_path, policy, rows)
    assert ("PNG",) in model["defined"]
    assert ("JPEG", "host") in model["need"]
    assert ("JPEG", "host") in model["use_bundle"]
    assert (("JPEG",) in model["defined"]) == (policy == "available")
    assert (("ImageIO", "host", "NO_JPEG") in model["flag"]) == (policy == "scoped")
    assert not model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_alias_only_offer_resolves_its_underlying_declaration(tmp_path, policy):
    model = evaluate(
        tmp_path,
        policy,
        {
            "av_offer": [("Alias", "host", "bundle", "alias")],
            "av_decl": [("bundle", "Real", "Real")],
            "av_alias": [("bundle", "Alias", "Real")],
            "request": [("Alias", "host")],
            "scope_root": [("Alias", "host")],
        },
    )
    assert model["defined"] == {("Alias",), ("Real",)}
    assert model["select"] == {("Alias", "host", "bundle")}
    assert model["use_bundle"] == {("bundle", "host")}
    assert not model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_canonical_offer_beats_alias_but_can_be_explicitly_pinned(tmp_path, policy):
    rows = combine(
        library("Pkg", "canonical"),
        library("Pkg", "adapter"),
        {"request": [("Pkg", "host")], "scope_root": [("Pkg", "host")]},
    )
    rows["av_offer"][1] = ("Pkg", "host", "adapter", "alias")
    first = tmp_path / "first"
    first.mkdir()
    model = evaluate(first, policy, rows)
    assert model["select"] == {("Pkg", "host", "canonical")}
    rows["pin"] = [("Pkg", "host", "adapter")]
    second = tmp_path / "second"
    second.mkdir()
    model = evaluate(second, policy, rows)
    assert model["select"] == {("Pkg", "host", "adapter")}
    assert not model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_ambiguity_is_existence_without_a_selected_winner(tmp_path, policy):
    rows = combine(
        library("Pkg", "one"),
        library("Pkg", "two"),
        {"request": [("Pkg", "host")], "scope_root": [("Pkg", "host")]},
    )
    model = evaluate(tmp_path, policy, rows)
    assert ("Pkg",) in model["defined"]
    assert not model["select"]
    assert ("ambiguous", "Pkg", "host") in model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_two_pins_fail_instead_of_choosing_by_order(tmp_path, policy):
    rows = combine(
        library("Pkg", "one"),
        library("Pkg", "two"),
        {"request": [("Pkg", "host")], "pin": [("Pkg", "host", "one"), ("Pkg", "host", "two")]},
    )
    model = evaluate(tmp_path, policy, rows)
    assert ("two pins", "Pkg", "host") in model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_included_names_expand_a_file_pin_and_keep_namespaces(tmp_path, policy):
    rows = combine(
        library("Pkg::root", "manifest"),
        library("Pkg::leaf", "exports"),
        library("Pkg::leaf", "other"),
        {
            "av_include": [("manifest", "exports"), ("exports", "manifest")],
            "pin": [("Pkg::root", "host", "manifest")],
            "scope_root": [("Pkg::root", "host")],
            "request": [("Pkg::leaf", "host")],
        },
    )
    model = evaluate(tmp_path, policy, rows)
    assert ("Pkg::leaf", "host", "exports") in model["select"]
    assert ("Pkg::leaf", "host", "other") not in model["select"]
    assert ("Pkg::leaf",) in model["defined"]
    assert not model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_verified_duplicate_does_not_make_a_second_candidate(tmp_path, policy):
    rows = combine(
        library("Pkg", "cps"),
        library("Pkg", "manifest"),
        {"av_same": [("cps", "manifest")], "request": [("Pkg", "host")]},
    )
    model = evaluate(tmp_path, policy, rows)
    assert model["select"] == {("Pkg", "host", "manifest")}
    assert not model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_source_alias_precedes_provider_selection(tmp_path, policy):
    rows = combine(
        library("Pkg", "installed"),
        {
            "source": [("local", "local")],
            "source_alias": [("Pkg", "local")],
            "request": [("Pkg", "host")],
        },
    )
    model = evaluate(tmp_path, policy, rows)
    assert not model["select"]
    assert not model["use_bundle"]
    assert model["defined"] == {("Pkg",), ("local",)}
    assert not model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
@pytest.mark.parametrize("explicit_file", [False, True])
def test_unused_bad_metadata_is_silent_until_explicitly_imported(tmp_path, policy, explicit_file):
    rows = combine(
        library("bad"),
        {
            "av_bad": [("bad",)],
            "source": [("app", "app")],
            "request": [("app", "host")],
            "explicit_file": [("bad", "host")] if explicit_file else [],
        },
    )
    model = evaluate(tmp_path, policy, rows)
    assert (("malformed metadata", "bad", "host") in model["error"]) == explicit_file
    assert ("bad",) not in model["defined"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_selected_bad_metadata_fails(tmp_path, policy):
    rows = combine(library("bad"), {"av_bad": [("bad",)], "request": [("bad", "host")]})
    model = evaluate(tmp_path, policy, rows)
    assert ("malformed metadata", "bad", "host") in model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_explicit_missing_file_is_not_silently_ignored(tmp_path, policy):
    model = evaluate(
        tmp_path,
        policy,
        {"explicit_file": [("missing-file", "host")], "av_bad": [("missing-file",)]},
    )
    assert model["error"] == {("malformed metadata", "missing-file", "host")}


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_adapter_declarations_bootstrap_without_query_activation(tmp_path, policy):
    rows = combine(
        library("LLVM", "adapter"),
        {
            "scope_root": [("LLVM", "host")],
            "request": [("LLVM::Support", "host")],
            "av_module": [("adapter", "llvm-module-id")],
            "av_module_decl": [("llvm-module-id", "LLVM::Support", "LLVM::Support")],
            "av_module_flag": [("llvm-module-id", "LLVM::Support", "LLVM_FLAG")],
        },
    )
    model = evaluate(tmp_path, policy, rows)
    assert ("LLVM::Support",) in model["defined"]
    assert ("LLVM::Support", "host", "LLVM_FLAG") in model["flag"]
    assert not model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_positive_dependency_cycle_closes_without_package_rounds(tmp_path, policy):
    rows = combine(
        library("one"),
        library("two"),
        {
            "request": [("one", "host")],
            "scope_root": [("one", "host")],
            "av_dep": [("one", "one", "two"), ("two", "two", "one")],
        },
    )
    model = evaluate(tmp_path, policy, rows)
    assert model["need"] == {("one", "host"), ("two", "host")}
    assert not model["error"]


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_negative_definition_activation_cycle_is_rejected(tmp_path, policy):
    evaluate(
        tmp_path,
        policy,
        combine(library("one"), {"request": [("one", "host")]}),
        'defined("late") :- need("one",_).\nneed("one","host") :- !defined("late").',
        reject=True,
    )


def test_even_dormant_unstratified_executable_code_invalidates_union(tmp_path):
    evaluate(
        tmp_path,
        "available",
        {},
        ".decl dormant()\ndormant() :- !dormant().",
        reject=True,
    )


def test_conflicting_installation_code_invalidates_union_even_with_a_pin(tmp_path):
    evaluate(
        tmp_path,
        "available",
        combine(
            library("Pkg", "one"),
            library("Pkg", "two"),
            {"pin": [("Pkg", "host", "one")]},
        ),
        ".decl installation(value:symbol)\n.decl installation(value:number)",
        reject=True,
        diagnostic="redefinition",
    )


def test_query_dependent_selection_is_rejected(tmp_path):
    evaluate(
        tmp_path,
        "available",
        {},
        'candidate("late","host","late") :- !need("late","host").',
        reject=True,
    )


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_architecture_validates_per_platform_and_does_not_select_an_alternative(tmp_path, policy):
    rows = combine(
        library("Pkg", "host-install"),
        library("Pkg", "target-install", "target"),
        {
            "request": [("Pkg", "host"), ("Pkg", "target")],
            "scope_root": [("Pkg", "host")],
            "av_file": [("host-install", "host.a"), ("target-install", "target.a")],
            "res_arch": [("host.a", "host", "yes"), ("target.a", "target", "no")],
        },
    )
    model = evaluate(tmp_path, policy, rows)
    assert model["select"] == {
        ("Pkg", "host", "host-install"),
        ("Pkg", "target", "target-install"),
    }
    assert model["error"] == {("wrong architecture", "target.a", "target")}


@pytest.mark.parametrize("policy", ["available", "scoped"])
def test_one_translated_program_serves_guarded_answers_and_ignores_unrequested_rows(
    tmp_path, policy
):
    path = tmp_path / "model.dl"
    path.write_text(program())
    base = combine(
        library("Pkg"),
        {"request": [("Pkg", "host")], "av_file": [("Pkg", "lib.a")]},
    )
    inputs = []
    for index, answers in enumerate(
        [[], [("lib.a", "host", "yes")], [("lib.a", "host", "yes"), ("unused", "host", "no")]]
    ):
        facts, out = tmp_path / f"facts-{index}", tmp_path / f"out-{index}"
        materialize(facts, policy, {**base, "res_arch": answers})
        out.mkdir()
        inputs.append(f"{facts}\t{out}\n")
    result = subprocess.run(
        [str(souffle_path()), "-w", "--serve", str(path)],
        input="".join(inputs),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("souffle-serve: ready") == 1
    assert result.stdout.count("souffle-serve: done") == 3
    assert not read_rows(tmp_path / "out-2/error.csv")
    for relation in OUTPUTS:
        assert set(read_rows(tmp_path / f"out-1/{relation}.csv")) == set(
            read_rows(tmp_path / f"out-2/{relation}.csv")
        )
