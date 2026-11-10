# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Halide hybrid sketch's component join is gated: tied request indices are
an error, not 2^n strings."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from buildlog.paths import souffle_path

RULES = Path(__file__).resolve().parents[1] / "case-studies/halide/phase1/hybrid/halide_llvm.dl"
INPUTS = [
    "tool_word",
    "lld_version",
    "lld_root",
    "clang_version",
    "want_component",
    "imp_target",
    "imp_dep",
    "imp_prop",
    "imp_location",
]

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


def run(tmp_path, components):
    facts = {name: [] for name in INPUTS}
    facts["tool_word"] = [
        ("version", 0, "22"),
        ("version", 1, "1"),
        ("version", 2, "8"),
        ("shared-mode", 0, "shared"),
        ("targets-built", 0, "WebAssembly"),
    ] + [("targets-built", i + 1, c) for i, (c, _) in enumerate(components)]
    facts["lld_version"] = [("22.1.8",)]
    facts["lld_root"] = [("/lld",)]
    facts["clang_version"] = [("22.1.8",)]
    facts["want_component"] = [(c, n, 1) for c, n in components]
    for name, rows in facts.items():
        (tmp_path / f"{name}.facts").write_text(
            "".join("\t".join(map(str, r)) + "\n" for r in rows)
        )
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(
        [str(souffle_path()), "-F", str(tmp_path), "-D", str(out), str(RULES)],
        check=True,
        capture_output=True,
        timeout=120,
    )

    def rows(rel):
        f = out / f"{rel}.csv"
        return f.read_text().splitlines() if f.exists() else []

    return rows("res_probe_setting"), rows("error")


def test_distinct_ranks_join_in_request_order(tmp_path):
    settings, errors = run(tmp_path, [("X86", 0), ("ARM", 1), ("AArch64", 2)])
    assert "halide_llvm.components\tX86;ARM;AArch64" in settings
    assert errors == []


def test_duplicate_ranks_are_an_error_and_not_folded(tmp_path):
    comps = [(f"C{i:02d}", i // 2) for i in range(24)]  # pairs share an index: 2^12 strings
    settings, errors = run(tmp_path, comps)
    assert not [s for s in settings if s.startswith("halide_llvm.components")]
    assert any("share a request index" in e for e in errors)


def test_a_component_requested_twice_is_an_error(tmp_path):
    settings, errors = run(tmp_path, [("X86", 0), ("X86", 1), ("ARM", 2)])
    assert not [s for s in settings if s.startswith("halide_llvm.components")]
    assert any("requested twice" in e for e in errors)
