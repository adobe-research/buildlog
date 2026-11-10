# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Source drift of the manually audited Lean core-invariant transcription."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "formal" / "lean" / "scripts" / "check_core_rules.py"
CHECK = runpy.run_path(str(SCRIPT))


def test_shipped_core_matches_lean_transcription_snapshot():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rule_edit_and_new_tool_dependency_source_are_detected():
    extract = CHECK["extract_rules"]
    expected = {"core.dl": "tool_dep(t,c,g) :- tool_ref(t,c,a,r,u), tool_resolves(t,r,g)."}
    changed = {
        "core.dl": expected["core.dl"].replace("tool_resolves(t,r,g)", "tool_resolves(t,r,h)")
    }
    added = expected | {"new.dl": "tool_dep(t,c,g) :- invented(t,c,g)."}
    baseline = extract(expected)
    assert extract(changed) != baseline
    assert extract(added) != baseline
    assert (
        extract(expected | {"new.dl": "invented(t,c,g), tool_dep(t,c,g) :- another(t,c,g)."})
        != baseline
    )
    assert "shipped" in CHECK["differences"](extract(added), baseline)


def test_comments_layout_and_string_literals_are_handled_separately():
    extract = CHECK["extract_rules"]
    plain = 'error("toolchain pinned twice", cat("x.//y",a)) :- toolchain_pin(l,e,p,a).'
    noisy = (
        "// ignore tool_dep(t,c,g) :- bogus(t,c,g).\n"
        'error( "toolchain pinned twice", /* a comment */ cat("x.//y", a))\n'
        " :- toolchain_pin(l,e,p,a). // another comment\n"
    )
    assert extract({"one.dl": plain}) == extract({"one.dl": noisy})
    assert extract({"one.dl": plain.replace("x.//y", "other")}) != extract({"one.dl": plain})


def test_snapshot_covers_both_pin_diagnostics_lists_and_built_toolchains():
    snapshot = json.loads(CHECK["EXPECTED"].read_text())
    all_rules = "\n".join(rule for rules in snapshot.values() for rule in rules)
    assert all_rules.count('error ( "toolchain pinned twice"') == 2
    assert "action_run_item" in all_rules
    assert "toolchain_tool_target" in all_rules
    assert "mx = min ord" in all_rules
    assert "config ( e ) :- exec_config" in all_rules
    assert "needed ( g , e ) :- tool_dep" in all_rules
    assert ". type Cfg = [ platform : symbol , settings : Settings ]" in all_rules
    assert "export key both spelled and unspelled" in all_rules
    assert "export_value_spelling" in all_rules
    assert "export_rules_lossy" in all_rules
    assert "setting key without exec policy" in all_rules
    assert "setting value is reserved" in all_rules
    assert "setting key declared at two positions" in all_rules
    assert "setting_before" in all_rules
    assert "setting_rank" in all_rules
    assert 'f != "inherit"' in all_rules
    assert "exec setting outside domain" in all_rules
    assert "exec setting given twice" in all_rules
    assert "setting default outside domain" in all_rules
    assert all_rules.count('error ( "setting key with two exec policies"') == 2
    assert "decl_value ( n , k , v )" in all_rules
    assert "decl_suffix ( n , i , [ [ k , v ] , s ] )" in all_rules
    assert "decl_settings ( t ) :- decl_settings" in all_rules
