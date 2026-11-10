# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""zlib case study: configure checks as discovery facts.

CMakeLists.txt's five checks are buildlog-cpp cc_check questions
(spec.toml). Their answers are probe results, keyed by the question's
content and the toolchain's identity, and cached. The definitions they decide
(HAVE_HIDDEN, NO_FSEEKO, _LARGEFILE64_SOURCE) are conditional properties;
`buildlog why` leads from such a definition back to the probe result. A
stated answer (cc_check_known in a user file) replaces the probe without
changing the rules that consume it.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from buildlog.engine import configure_project
from buildlog.facts import read_rows
from tests import host
from tests.test_cpp_mechanisms import cc_check_fields
from tests.zlib_case import (
    BASELINE,
    PRESET,
    PROJECT,
    REL,
    baseline_commands,
    commands,
    compiles,
    configure,
    needs_zlib,
)

pytestmark = needs_zlib

# spec.toml's check name -> CMakeCache.txt's variable of the same question.
UNSTATED = {"zlib_example64", "zlib_static_example64", "infcover"}

CHECKS = {
    "HAVE_OFF64_T": "HAVE_OFF64_T",
    "HAVE_FSEEKO": "HAVE_FSEEKO",
    "HAVE_STDARG_H": "HAVE_STDARG_H",
    "HAVE_UNISTD_H": "HAVE_UNISTD_H",
    "HAVE___ATTR__VIS_HIDDEN": "HAVE___ATTR__VIS_HIDDEN",
}
SUBJECT = {
    "off64_t": "HAVE_OFF64_T",
    "fseeko": "HAVE_FSEEKO",
    "stdarg.h": "HAVE_STDARG_H",
    "unistd.h": "HAVE_UNISTD_H",
}


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    build = tmp_path_factory.mktemp("zlib") / "build"
    first = configure(build)
    return build, first


def _answers(build) -> dict[str, str]:
    """Check name -> yes | no, from the probe results (the request id is the
    question: toolchain, exec, target, lang, kind, subject, headers, args)."""
    out = {}
    for req, result in read_rows(build / "env-facts" / "res_cc_check.facts"):
        kind, subject = cc_check_fields(req)[4:6]
        out["HAVE___ATTR__VIS_HIDDEN" if kind == "compiles" else SUBJECT[subject]] = result
    return out


def _baseline_checks() -> dict[str, str]:
    rows = dict(
        line.split(":INTERNAL=")
        for line in (BASELINE / "checks.txt").read_text().splitlines()
        if ":INTERNAL=" in line
    )
    # check_type_size's HAVE_ is TRUE (1 on macOS's record), the others 1.
    return {name: "yes" if rows[var] in ("1", "TRUE") else "no" for name, var in CHECKS.items()}


# The Linux baseline is GCC's (CMake's default cc there); GCC ignores the
# visibility attribute on a static declaration, which -Werror makes a "no",
# clang accepts it. A clang run compares that one answer with its own.
FAMILY_ANSWERED = {"HAVE___ATTR__VIS_HIDDEN"} if host.LINUX and host.FAMILY != "gcc" else set()


def _hidden(build) -> bool:
    return _answers(build)["HAVE___ATTR__VIS_HIDDEN"] == "yes"


def needs_hidden(build):
    if not _hidden(build):
        pytest.skip("the toolchain answers no to HAVE___ATTR__VIS_HIDDEN: no -DHAVE_HIDDEN")


def test_answers_match_cmakes_check_results(build):
    b, _ = build
    ours, theirs = _answers(b), _baseline_checks()
    for k in FAMILY_ANSWERED:
        del ours[k], theirs[k]
    assert ours == theirs


def test_defines_match_the_baseline_except_cmakes_no_fseeko(build):
    b, _ = build
    ours, theirs = compiles(commands(b)), compiles(baseline_commands())
    # CMake builds these only where off64_t exists (and infcover only with
    # GCC); the spec does not state them (the README's ledger).
    theirs = {k: v for k, v in theirs.items() if k[0] not in UNSTATED}
    assert set(ours) == set(theirs) and len(ours) == 34
    for key, cmake in theirs.items():
        # CMake's $<$<BOOL:NOT:${HAVE_FSEEKO}>:NO_FSEEKO> is always true (the
        # README's ledger); zlib's intent, NO_FSEEKO where fseeko is
        # missing, is what the spec states, so it is absent here.
        decided = {"HAVE_HIDDEN"} if FAMILY_ANSWERED else set()
        assert ours[key]["defines"] - decided == cmake["defines"] - {"NO_FSEEKO"} - decided, key
        assert ("HAVE_HIDDEN" in ours[key]["defines"]) == (
            _hidden(b) and key[0] in ("zlib", "zlibstatic", "minigzip", "zlib_static_example")
        ), key


def _why_leaves(build, flag: str, output: str, *extra: str) -> str:
    return subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "buildlog",
            "why",
            "-C",
            str(build),
            "--flag",
            flag,
            "--output",
            output,
            "--leaves",
            *extra,
            str(PROJECT),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_why_have_hidden_leads_to_the_probe_result(build):
    b, _ = build
    needs_hidden(b)
    out = _why_leaves(b, "-DHAVE_HIDDEN", f"obj/{REL}/zlib/adler32.c.o")
    # The probe's answer, with its request (the whole question) ...
    probe = [line for line in out.splitlines() if line.strip().startswith("res_cc_check(")]
    assert len(probe) == 1 and '"yes")' in probe[0] and "← probe cc_check request" in probe[0]
    assert ":8:compiles:" in probe[0] and ":8:stdlib.h:-Werror" in probe[0]
    # ... and the spec lines that asked it and consumed it.
    for fact in (
        'cc_check_compiles("HAVE___ATTR__VIS_HIDDEN"',
        'target_when("zlib", "cc.check.HAVE___ATTR__VIS_HIDDEN"',
    ):
        assert fact in out, fact


def test_why_have_hidden_separates_the_answer_from_the_demand(build):
    b, _ = build
    needs_hidden(b)
    obj = f"obj/{REL}/zlib/adler32.c.o"
    # Why the flag holds: the check's request is pruned, and cited by its
    # result's line; which target happened to need a C toolchain (minigzip)
    # is not part of it, with or without --with-demand (target demand only).
    for extra in ([], ["--with-demand"]):
        answer = _why_leaves(b, "-DHAVE_HIDDEN", obj, *extra)
        assert 'c_source("minigzip"' not in answer
        assert 'request_all("minigzip")' not in answer
        assert "[asked: probe cc_check request" not in answer
        (cited,) = [ln for ln in answer.splitlines() if ln.strip().startswith("res_cc_check(")]
        assert "← probe cc_check request" in cited
    # Why the check ran: the demand behind the requests under that proof.
    demand = _why_leaves(b, "-DHAVE_HIDDEN", obj, "--demand", "cc_check")
    assert demand.splitlines()[0].startswith("req_cc_check(")
    assert 'cc_check_compiles("HAVE___ATTR__VIS_HIDDEN"' in demand
    # Which C target Souffle proves the demand through is its choice (it was
    # minigzip; since zconf.h is the library's, it is zlib): only that some
    # requested target's C sources demand it.
    everything = _why_leaves(b, "-DHAVE_HIDDEN", obj, "--demand", "*")
    assert "request_all(" in everything
    assert len(everything.splitlines()) > len(answer.splitlines())


def test_reconfigure_reuses_the_cached_answers(build):
    b, first = build
    assert first["executed"] > 0
    cache = json.loads((b / "probe-cache.json").read_text())
    assert sum(json.loads(k)[0] == "cc_check" for k in cache) == 5
    again = configure(b)
    assert again["executed"] == 0 and again["cache_hits"] >= 5


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_a_stated_answer_replaces_the_probe(tmp_path, answer):
    # The user states the hidden-visibility answer for apple-clang on this
    # platform (a toolchain-knowledge row would look the same). That
    # question is then not probed, the rules that consume the answer are
    # unchanged, and `why` leads to the stated row.
    known = tmp_path / "known.toml"
    known.write_text(
        "cc_check_known = [[\n"
        '  "apple-clang", "arm64-macos", "arm64-macos", "c", "compiles",\n'
        '  \'static void f(void) __attribute__ ((visibility("hidden"))); '
        "int main(void) {return 0;}',\n"
        f'  "stdlib.h", "-Werror", "{answer}",\n'
        "]]\n"
    )
    b = tmp_path / "build"
    _, metrics = configure_project(PROJECT, b, user_files=[PRESET, known])
    assert metrics["errors"] == []
    probed = {
        cc_check_fields(req)[4] for req, _ in read_rows(b / "env-facts" / "res_cc_check.facts")
    }
    assert probed == {"include", "type", "function"}
    lines = commands(b)
    assert any("-DHAVE_HIDDEN" in c for c in lines) == (answer == "yes")
    if answer == "yes":
        out = _why_leaves(b, "-DHAVE_HIDDEN", f"obj/{REL}/zlib/adler32.c.o")
        assert "res_cc_check(" not in out
        assert any(
            line.strip().startswith("cc_check_known(") and "known.toml:" in line
            for line in out.splitlines()
        ), out
