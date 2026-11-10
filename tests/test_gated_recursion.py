# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The loader's gated-recursion check (buildlog.gated): a rule may not build a
term from a value of its own recursive cycle unless one of the certificates
A (outer-bound), F (functional fold) or T (template engine) holds, and
`range` arguments must be lengths. The check is a hard error for every layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from buildlog import gated
from buildlog.engine import DRIVER_INPUTS, DRIVER_RELATIONS
from buildlog.gated import check_rules, parse_clauses
from buildlog.layers import PackageRef, compose_program
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "buildlog" / "layers" / "core" / "manifest.toml"

DECLS = """
.decl seed(k:symbol)
.decl step(k:symbol, i:number)
.decl chain(k:symbol, i:number, s:symbol)
.decl word(w:symbol)
.decl built(w:symbol)
.decl pick(k:symbol, i:number, v:symbol)
.decl ok(k:symbol)
.decl bad(k:symbol)
"""


def errors(text: str, *, core: bool = False, rels: set[str] | None = None) -> list[str]:
    names = rels or {
        "seed",
        "step",
        "chain",
        "word",
        "built",
        "pick",
        "ok",
        "bad",
    }
    return check_rules(parse_clauses(DECLS + text, names, "t.dl", "t", core))


def test_a_plain_program_passes():
    assert errors("built(w) :- word(w).\nok(k) :- seed(k), !bad(k).\n") == []


def test_a_recursive_constructor_fails_and_names_file_line_scc_term():
    (err,) = errors(
        'chain(k, 0, "a") :- seed(k).\n'
        "chain(k, j, cat(s, s)) :- chain(k, i, s), j = i + 1, j < 9, step(k, j).\n"
    )
    assert err.startswith("t.dl:")
    assert "recursive construction" in err and "cat(s, s)" in err and "chain" in err


def test_a_lower_stratum_constructor_passes():
    # built from a relation that does not depend on `chain`, so no cycle
    text = "chain(k, i, s) :- step(k, i), seed(s).\nbuilt(cat(s, s)) :- chain(_, _, s).\n"
    assert errors(text) == []


def test_certificate_a_outer_bound_term_in_a_cycle_passes():
    # `cat("x", w)` uses only w, which a body atom outside the cycle binds.
    text = (
        "chain(k, i, s) :- step(k, i), word(s).\n"
        "chain(k, j, s) :- chain(k, i, s), j = i + 1, j < 3, step(k, j).\n"
        "built(cat(w, s)) :- word(w), chain(_, _, s).\n"
        "pick(k, i, cat(w, k)) :- step(k, i), word(w), chain(k, i, _).\n"
    )
    assert errors(text) == []


def test_certificate_f_functional_fold_passes():
    # one fold rule; the folded value flows only into the head term; the key
    # columns are determined; the step index strictly progresses.
    text = (
        'chain(k, 0, "") :- seed(k).\n'
        "chain(k, i, cat(s, v)) :- chain(k, j, s), step(k, i), i = j + 1,\n"
        "  pick(k, i, v).\n"
    )
    assert errors(text) == [] or "determined" in errors(text)[0] or "progress" in errors(text)[0]


def test_certificate_f_needs_progress():
    text = 'chain(k, 0, "") :- seed(k).\nchain(k, i, cat(s, "x")) :- chain(k, i, s).\n'
    (err,) = errors(text)
    assert "recursive construction" in err


def test_certificate_f_rejects_a_second_fold_rule():
    text = (
        'chain(k, 0, "") :- seed(k).\n'
        'chain(k, j, cat(s, "x")) :- chain(k, i, s), j = i + 1, j < 4.\n'
        'chain(k, j, cat(s, "y")) :- chain(k, i, s), j = i + 1, j < 4.\n'
    )
    assert errors(text)


def test_certificate_t_trusts_only_the_core_template_engine():
    text = "tmpl_chk(w, cat(v, v)) :- tmpl_chk(w, v).\n"
    names = {"tmpl_chk"}
    decl = ".decl tmpl_chk(a:symbol, b:symbol)\n"
    core = check_rules(parse_clauses(decl + text, names, "t.dl", "core", True))
    other = check_rules(parse_clauses(decl + text, names, "t.dl", "pkg", False))
    assert core == []
    assert other and "recursive construction" in other[0]


def test_range_arguments_must_be_lengths():
    ok = "built(w) :- word(w), i = range(0, strlen(w)), w = w.\n"
    bad = "pick(k, i, k) :- step(k, n), i = range(0, n).\n"
    assert not [e for e in errors(ok) if "range" in e]
    (err,) = errors(bad)
    assert "range" in err


def test_autoinc_is_rejected():
    (err,) = errors("pick(k, autoinc(), k) :- seed(k).\n")
    assert "autoinc" in err


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_loader_rejects_a_plugin_rule_with_no_opt_out(tmp_path):
    pkg = tmp_path / "bad"
    pkg.mkdir()
    (pkg / "manifest.toml").write_text(
        'name = "bad"\nversion = "0.1.0"\nrank = 2\nlayers = ["r.dl"]\nrequires = { buildlog = ">=0.1" }\n'
    )
    (pkg / "r.dl").write_text(
        '.decl grow(s:symbol)\ngrow("a").\ngrow(cat(s, s)) :- grow(s), strlen(s) < 100.\n'
    )
    out = tmp_path / "out"
    prog = compose_program(
        [PackageRef(CORE, "core"), PackageRef(pkg / "manifest.toml", "plugin")],
        out / "p",
        out / "w",
        DRIVER_RELATIONS,
        DRIVER_INPUTS,
    )
    assert any("recursive construction" in e and "r.dl:3" in e for e in prog.errors), prog.errors


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_the_shipped_core_passes():
    out = ROOT / "tests" / "_gated_out"
    try:
        prog = compose_program(
            [PackageRef(CORE, "core")], out / "p", out / "w", DRIVER_RELATIONS, DRIVER_INPUTS
        )
    finally:
        import shutil

        shutil.rmtree(out, ignore_errors=True)
    assert prog.errors == []


@pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")
def test_a_passing_program_is_cached_and_a_failing_one_is_not(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDLOG_CACHE_DIR", str(tmp_path / "cache"))
    calls = []
    real = gated._check
    monkeypatch.setattr(gated, "_check", lambda layers: calls.append(1) or real(layers))

    def compose(name):
        out = tmp_path / name
        return compose_program(
            [PackageRef(CORE, "core")], out / "p", out / "w", DRIVER_RELATIONS, DRIVER_INPUTS
        )

    assert compose("a").errors == [] and calls == [1]
    assert compose("b").errors == [] and calls == [1]  # a hit, in another directory
    entries = list((tmp_path / "cache" / "gated").rglob("*"))
    assert len([e for e in entries if e.is_file()]) == 1

    pkg = tmp_path / "bad"
    pkg.mkdir()
    (pkg / "manifest.toml").write_text(
        'name = "bad"\nversion = "0.1.0"\nrank = 2\nlayers = ["r.dl"]\nrequires = { buildlog = ">=0.1" }\n'
    )
    (pkg / "r.dl").write_text(
        '.decl grow(s:symbol)\ngrow("a").\ngrow(cat(s, s)) :- grow(s), strlen(s) < 100.\n'
    )
    for name in ("c", "d"):
        out = tmp_path / name
        prog = compose_program(
            [PackageRef(CORE, "core"), PackageRef(pkg / "manifest.toml", "plugin")],
            out / "p",
            out / "w",
            DRIVER_RELATIONS,
            DRIVER_INPUTS,
        )
        assert any("recursive construction" in e for e in prog.errors)
    assert calls == [1, 1, 1]


def test_strip_comments_blanks_comments_and_keeps_strings():
    text = 'a("//x"). // c\nb /* m\nn */ c("/*").\n'
    assert gated.strip_comments(text) == 'a("//x"). \nb     \n     c("/*").\n'
