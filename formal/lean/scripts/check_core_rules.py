# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check source drift of the manually transcribed Lean core rule subset.

This is a source-snapshot check, NOT a verified Souffle parser or translator.
Comments/layout are ignored, but literals, premises, guards, head arguments,
aggregate syntax and declaration signatures are retained. All core .dl files
are scanned so a new clause for a protected derived relation is caught.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CORE = ROOT / "src" / "buildlog" / "layers" / "core"
EXPECTED = Path(__file__).with_name("core_rules.expected.json")

# Include every defining clause for these relations, not selected line ranges.
DERIVED = {
    "decl_has",
    "decl_value",
    "setting_before",
    "setting_rank",
    "setting_at",
    "decl_value1",
    "decl_suffix",
    "decl_cfg",
    "mconfig",
    "config_slice",
    "has_exec_policy",
    "exec_overridden",
    "exec_bad",
    "exec_value",
    "exec_settings",
    "exec_config",
    "config_platform",
    "toolchain_built",
    "tc_candidate",
    "pinned",
    "tc_select",
    "tc_used",
    "tc_for",
    "has_tc",
    "scoped",
    "scope_name",
    "project_name",
    "name_candidate",
    "tool_ref",
    "tool_resolves",
    "tool_resolved",
    "tool_dep",
    "has_run_artifact",
    "export_format_named",
    "export_format_in",
    "export_format_off",
    "export_format_active",
    "has_value_spelling",
    "has_spelling",
    "export_spelled",
    "export_lossy",
    "export_rules_lossy",
}
INPUTS = {
    "setting_key",
    "setting_default",
    "config_decl",
    "config_decl_setting",
    "platform",
    "platform_slice",
    "nkeys",
    "exec_setting",
    "setting_value",
    "setting_exec_inherit",
    "setting_exec_value",
    "decl_settings",
    "base_config",
    "host_platform",
    "cfg_name",
    "toolchain_runs_on",
    "toolchain_can_target",
    "toolchain_tool_target",
    "toolchain_pin",
    "tc_required",
    "target_action",
    "author_decision",
    "action_run",
    "action_run_item",
    "action_tc_tool",
    "target_toolchain",
    "target_id",
    "id_scope",
    "alias",
    "run_artifact",
    "needed",
    "export_fact",
    "export_format_file",
    "export_format_of",
    "export_format_omit",
    "export_format_skip",
    "export_format_components",
    "export_spelling",
    "export_value_spelling",
    "export_unspelled",
    "export_rules",
}
DIAGNOSTICS = {
    "setting value is reserved",
    "setting key declared at two positions",
    "setting key without default",
    "setting key without exec policy",
    "setting key with two exec policies",
    "setting default outside domain",
    "exec setting for an unknown key",
    "exec setting outside domain",
    "exec setting given twice",
    "two configurations share a name",
    "exec transition not a function",
    "exec transition not idempotent",
    "ambiguous toolchain (add a toolchain_pin)",
    "toolchain pin matches no candidate",
    "toolchain pinned twice",
    "no toolchain",
    "tool has nothing to run",
    "export key both spelled and unspelled",
    "export key both value-spelled and unspelled",
    "exported property has no spelling in an export format",
    "exported value has no spelling in an export format",
    "export format cannot state a usage requirement",
    "export format cannot carry shipped rules",
}
PATH_HEADS = {"action_arg", "action_arg_part", "action_in"}
BOUNDARY = {"config", "base_config", "decl_settings", "nkeys"}
TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[A-Za-z_]\w*|\d+(?:\.\d+)?|:-|!=|<=|>=|[^\s]')
START = re.compile(r"(?m)(?:^|(?<=\.))[ \t\r\n]*[a-z_]\w*\s*\(")
DECL = re.compile(r"(?m)^[ \t]*\.decl[ \t]+([A-Za-z_]\w*)[ \t]*\(")
TYPE_DECL = re.compile(r"(?m)^[ \t]*\.type[ \t]+(Setting|Settings|Cfg)\b[^\n]*")


def strip_comments(text: str) -> str:
    """Keep strings and line structure while removing // and /* */ comments."""
    result: list[str] = []
    pos = 0
    quoted = False
    while pos < len(text):
        char = text[pos]
        if quoted:
            result.append(char)
            pos += 1
            if char == "\\" and pos < len(text):
                result.append(text[pos])
                pos += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            result.append(char)
            pos += 1
        elif text.startswith("//", pos):
            end = text.find("\n", pos)
            pos = len(text) if end == -1 else end
            result.append(" ")
        elif text.startswith("/*", pos):
            end = text.find("*/", pos + 2)
            if end == -1:
                raise ValueError("unterminated block comment")
            result.append(" " + "\n" * text[pos : end + 2].count("\n"))
            pos = end + 2
        else:
            result.append(char)
            pos += 1
    return "".join(result)


def statement_end(text: str, start: int) -> int:
    """Find a clause's top-level final dot, respecting strings and aggregates."""
    depth = 0
    quoted = False
    escaped = False
    for pos in range(start, len(text)):
        char = text[pos]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "." and depth == 0:
            return pos + 1
    raise ValueError(f"unterminated clause starting {text[start : start + 70]!r}")


def head_relations(tokens: list[str]) -> set[str]:
    result: set[str] = set()
    depth = 0
    for pos, token in enumerate(tokens):
        if token == ":-":
            break
        if depth == 0 and pos + 1 < len(tokens) and tokens[pos + 1] == "(":
            result.add(token)
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
    return result


def selected(tokens: list[str]) -> bool:
    heads = head_relations(tokens)
    # Snapshot all non-tool needed sources too, as the documented module boundary.
    if heads & (DERIVED | BOUNDARY | {"needed"}):
        return True
    if (
        heads & {"error", "error_at", "warning"}
        and len(tokens) > 2
        and tokens[2].startswith('"')
        and json.loads(tokens[2]) in DIAGNOSTICS
    ):
        return True
    body = tokens[tokens.index(":-") + 1 :] if ":-" in tokens else []
    return bool(
        heads & PATH_HEADS
        and (
            "action_run" in body
            or "action_run_item" in body
            or ("action_tc_tool" in body and "toolchain_tool_target" in body)
        )
    )


def extract_rules(sources: Mapping[str, str]) -> dict[str, list[str]]:
    extracted: dict[str, list[str]] = {}
    for filename, source in sorted(sources.items()):
        text = strip_comments(source)
        entries: list[str] = []
        for type_decl in TYPE_DECL.finditer(text):
            line = type_decl[0]
            if line.count("[") != line.count("]"):
                raise ValueError(f"{filename}: multiline record type needs extractor review")
            entries.append(" ".join(TOKEN.findall(line)))
        for decl in DECL.finditer(text):
            if decl[1] in DERIVED | INPUTS | PATH_HEADS | BOUNDARY:
                # Core declarations are single-line; fail closed on a new format.
                end = text.find("\n", decl.start())
                line = text[decl.start() : end if end != -1 else len(text)]
                if line.count("(") != line.count(")"):
                    raise ValueError(f"{filename}: multiline declaration needs extractor review")
                entries.append(" ".join(TOKEN.findall(line)))
        pos = 0
        while match := START.search(text, pos):
            end = statement_end(text, match.start())
            tokens = TOKEN.findall(text[match.start() : end])
            if selected(tokens):
                entries.append(" ".join(tokens))
            pos = end
        if entries:
            extracted[filename] = sorted(entries)
    return extracted


def current_rules() -> dict[str, list[str]]:
    return extract_rules(
        {str(path.relative_to(CORE)): path.read_text() for path in sorted(CORE.rglob("*.dl"))}
    )


def differences(actual: dict[str, list[str]], expected: dict[str, list[str]]) -> str:
    def render(rules: dict[str, list[str]]) -> list[str]:
        return [
            f"{filename}: {rule}\n"
            for filename, entries in sorted(rules.items())
            for rule in entries
        ]

    return "".join(
        difflib.unified_diff(
            render(expected), render(actual), fromfile="expected", tofile="shipped"
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-snapshot", action="store_true", help="update ONLY after manually auditing Lean"
    )
    args = parser.parse_args()
    actual = current_rules()
    if args.write_snapshot:
        EXPECTED.write_text(json.dumps(actual, indent=2) + "\n")
        print(
            f"Wrote {EXPECTED.relative_to(ROOT)}; audit the Lean correspondence before committing."
        )
        return 0
    expected = json.loads(EXPECTED.read_text())
    diff = differences(actual, expected)
    if diff:
        print("Core invariant transcription drift; review Lean before updating the snapshot:\n")
        print(diff, end="")
        return 1
    print(
        f"Core invariant snapshot matches ({sum(map(len, actual.values()))} declarations/clauses)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
