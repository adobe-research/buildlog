# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery checks and seeded guards must not observe completion-gated errors."""

import pytest

from buildlog.errors import LoaderError
from buildlog.layers import PackageRef, compose_program
from buildlog.paths import souffle_path

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


def _compose(root, text, *, accepts=()):
    package = root / "plugin"
    package.mkdir()
    (package / "manifest.toml").write_text(
        'name = "check-deps"\nversion = "0.1.0"\nrank = 0\n'
        'layers = ["rules.dl"]\n'
        'exports = ["discovery_complete", "propagated_error", "helper"]\n'
        f"accepts = {list(accepts)!r}\n"
        'outputs = ["discovered_rule_package"]\n'
        '[[probe]]\nkind = "test"\nrequest = "req"\ndomain = "dom"\n'
        'result = ["res"]\nentry = "unused:probe"\n'
    )
    (package / "rules.dl").write_text(
        ".decl req(id:symbol)\n.decl dom(id:symbol)\n.decl res(id:symbol)\n"
        ".decl probe_pending(id:symbol)\n.decl discovery_complete()\n"
        ".decl root_error()\n.decl propagated_error()\n.decl helper(id:symbol)\n"
        ".decl discovered_rule_package(id:symbol, path:symbol)\n"
        'req("r").\nroot_error().\n'
        "discovery_complete() :- !probe_pending(_).\n"
        "propagated_error() :- root_error(), discovery_complete().\n" + text
    )
    program = compose_program(
        [PackageRef(package / "manifest.toml", "plugin")], root / "out", root / "ast"
    )
    if program.errors:
        raise LoaderError(program.errors)
    return program


@pytest.mark.parametrize(
    ("text", "root"),
    [
        ('dom("r") :- propagated_error().\n', "dom"),
        ('helper("r") :- propagated_error().\ndom(r) :- helper(r).\n', "dom"),
        ('dom("r") :- !propagated_error().\n', "dom"),
        ('dom("r") :- 0 = count : { propagated_error() }.\n', "dom"),
        (
            'dom("r").\ndiscovered_rule_package("r", "pkg") :- propagated_error().\n',
            "discovered_rule_package",
        ),
        ('dom("r") :- discovery_complete().\n', "dom"),
        ('#define BROKEN() propagated_error()\ndom("r") :- BROKEN().\n', "dom"),
    ],
)
def test_loader_rejects_sol_counterexample_and_other_check_reads(tmp_path, text, root):
    with pytest.raises(LoaderError, match="W4") as exc:
        _compose(tmp_path, text)
    assert any(
        e.startswith(f"{root} depends on completion-gated relation") for e in exc.value.errors
    )
    assert all(
        "must not depend on propagated errors or discovery_complete" in e for e in exc.value.errors
    )


def test_error_independent_domain_is_allowed(tmp_path):
    assert _compose(tmp_path, 'dom("r").\n').build is not None


@pytest.mark.parametrize(
    ("helper", "accepts"),
    [
        ('helper("r").\nhelper(id) :- req(id).\n', ()),
        ("helper(id) :- req(id).\n", ("helper",)),
        ("helper(id) :- dom(id), 0 = count : { req(id) }.\n", ()),
        ("helper(id) :- (req(id); dom(id)).\n", ()),
    ],
)
def test_guard_cannot_be_bypassed_by_facts_inputs_or_aggregate_scope(tmp_path, helper, accepts):
    with pytest.raises(LoaderError, match="res is read without its request"):
        _compose(
            tmp_path,
            'dom("r").\n' + helper + "discovered_rule_package(id, id) :- helper(id), res(id).\n",
            accepts=accepts,
        )


def test_macro_cannot_hide_an_unguarded_result_read(tmp_path):
    with pytest.raises(LoaderError, match="res is read without its request"):
        _compose(
            tmp_path,
            'dom("r").\n#define READ(id) res(id)\nhelper(id) :- dom(id), READ(id).\n',
        )
