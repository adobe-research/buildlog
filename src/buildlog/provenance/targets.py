# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Friendly `why` targets: a Ninja output path and/or a command-line token.

`--output obj/.../main.cpp.o` and `--flag -DHALIDE_WITH_EXCEPTIONS` are
mapped to the L0 tuples that put them in `build.ninja`, read from the
derived relations of the configured build (`action`, `action_out`,
`action_arg`, `action_seq`). `--output` also takes a target name, a source
file an action compiles, or an output's basename, narrowed by `--config`;
`--flag` also takes a token's basename (`clang++`) or a prefix
(`--spirv-core-grammar`) when no token is spelled exactly so. A bare target
name asks why the target is built (`needed`). Nothing is re-derived: the only Python logic
is the emitter's own spelling of a token (`prefix + value`, with `out:`
paths relative to the Ninja directory and `src:` paths relative to the
source root).
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

from ..errors import BuildlogError
from .souffle import Model
from .values import explain_text


@dataclass(frozen=True)
class Target:
    atom: str
    relation: str
    row: tuple


def _ninja_path(value: str) -> str:
    return value.removeprefix("out:")


def _path_matches(value: str, path: str) -> bool:
    """Does an L0 path value spell `path` on the Ninja command line?"""
    if value == path:
        return True
    if value.startswith("out:"):
        return value[4:] == path
    if value.startswith("src:"):
        rest = value[4:]
        return path == rest or path.endswith("/" + rest)
    return False


def _normal_output(model: Model, path: str) -> str:
    ninja_dir = model.builddir / "souffle"
    p = Path(path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(ninja_dir.resolve()).as_posix()
        except ValueError:
            return path
    return os.path.normpath(path)


def _atom(rel: str, row: tuple) -> str:
    return f"{rel}(" + ", ".join(explain_text(v) for v in row) + ")"


def _in_configs(model: Model, action: str, names: set[str] | None) -> bool:
    """Is an action's output under a directory named for one of `names`?"""
    if names is None:
        return True
    parts = _ninja_path(action).split("/")
    return any(n in parts for n in names)


def _loose_actions(model: Model, path: str, names: set[str] | None) -> set[str]:
    """Actions for a `--output` that is not an output path: a target's
    actions, the actions that compile a source file, or outputs with that
    basename."""
    tactions = {
        a
        for t, c, a in _rows(model, "target_action")
        if t == path and (names is None or _cfg_name(model, c) in names)
    }
    if tactions:
        return tactions
    compiled = {
        a
        for a, p, kind in model.derived_rows("action_in")
        if kind == "explicit"
        and (_path_matches(p, path) or (p.startswith("src:") and p.endswith("/" + path)))
        and _in_configs(model, a, names)
    }
    if compiled:
        return compiled
    outs = {a for a, _ in model.derived_rows("action")} | {
        a for a, _ in model.derived_rows("action_out")
    }
    return {a for a in outs if Path(_ninja_path(a)).name == path and _in_configs(model, a, names)}


def _rows(model: Model, rel: str) -> list[tuple[str, ...]]:
    try:
        return model.derived_rows(model.index.resolve(rel))
    except (KeyError, OSError):
        return []


def _cfg_name(model: Model, value: str) -> str:
    from .proof import cfg_text, legend

    if not hasattr(model, "_cfg_names"):
        model._cfg_names = legend(model)
    text = cfg_text(value)
    return model._cfg_names.get(text, text)


def config_filter(model: Model, config: str | None) -> set[str] | None:
    from .proof import config_names, match_config

    if config is None:
        return None
    names = match_config(model, config)
    if not names:
        by_name, decls = config_names(model)
        known = ", ".join(sorted(set(by_name) | set(decls))) or "none"
        raise BuildlogError(f"--config {config} names no configuration (known: {known})")
    return names


def needed_goals(model: Model, target: str, names: set[str] | None) -> list[str]:
    """`needed(target, c)` for each configuration (in `names`) it is built in."""
    goals = []
    for t, c in _rows(model, "needed"):
        if t == target and (names is None or _cfg_name(model, c) in names):
            from .proof import cfg_text

            goals.append(f"{model.index.resolve('needed')}({explain_text(t)}, {cfg_text(c)})")
    return sorted(goals)


def is_target(model: Model, name: str) -> bool:
    return any(t == name for t, *_ in _rows(model, "needed")) or any(
        t == name for t, *_ in _rows(model, "target_action")
    )


def find_targets(
    model: Model, flag: str | None, output: str | None, config: str | None = None
) -> list[Target]:
    actions = {a: label for a, label in model.derived_rows("action")}
    names = config_filter(model, config)
    wanted: set[str] | None = None
    loose = False
    out_targets: list[Target] = []
    if output is not None:
        path = _normal_output(model, output)
        wanted = {a for a in actions if _path_matches(a, path)}
        for a in sorted(wanted):
            out_targets.append(Target(_atom("action", (a, actions[a])), "action", (a, actions[a])))
        for a, p in model.derived_rows("action_out"):
            if _path_matches(p, path):
                wanted.add(a)
                out_targets.append(Target(_atom("action_out", (a, p)), "action_out", (a, p)))
        if not wanted:
            wanted = _loose_actions(model, output, names)
            loose = True
            for a in sorted(wanted & set(actions)):
                out_targets.append(
                    Target(_atom("action", (a, actions[a])), "action", (a, actions[a]))
                )
        if not wanted:
            known = sorted(_ninja_path(a) for a in actions)
            close = difflib.get_close_matches(path, known, n=3, cutoff=0.4)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            raise BuildlogError(f"no action produces {path}{hint}")
    if flag is None:
        return out_targets
    # (spelled tokens, whether the value is a path, the matched tuple and its
    # companions) for every argument of a wanted action.
    candidates: list[tuple[list[str], str, str, bool, Target, list[Target]]] = []
    basis: dict[tuple[str, int, str], list[tuple]] = {}
    for a, r, v, b in model.derived_rows("action_arg_basis"):
        basis.setdefault((a, int(r), v), []).append((a, int(r), v, b))
    for a, rank, prefix, value in model.derived_rows("action_arg"):
        if (wanted is not None and a not in wanted) or not _in_configs(model, a, names):
            continue
        row = (a, int(rank), prefix, value)
        # Why the argument may stand there (a layer's action_arg_basis).
        extra = [
            Target(_atom("action_arg_basis", b), "action_arg_basis", b)
            for b in basis.get((a, int(rank), value), [])
        ]
        candidates.append(
            (
                _spell(prefix, value),
                prefix,
                value,
                value.startswith(("out:", "src:")),
                Target(_atom("action_arg", row), "action_arg", row),
                extra,
            )
        )
    spelled_seq = {
        (a, int(r), i): (p, v) for a, r, i, p, v in model.derived_rows("action_seq_spelled")
    }
    before_seq: dict[tuple[str, int], list[tuple]] = {}
    for a, r, x, y in model.derived_rows("action_seq_before"):
        before_seq.setdefault((a, int(r)), []).append((a, int(r), x, y))
    for a, rank, item in model.derived_rows("action_seq"):
        if (wanted is not None and a not in wanted) or not _in_configs(model, a, names):
            continue
        prefix, value = spelled_seq.get((a, int(rank), item), ("", item))
        row = (a, int(rank), item)
        # Where the item stands in its segment: the ordering facts about it.
        extra = [
            Target(_atom("action_seq_before", b), "action_seq_before", b)
            for b in before_seq.get((a, int(rank)), [])
            if item in (b[2], b[3])
        ]
        candidates.append(
            (
                _spell(prefix, value),
                prefix,
                value,
                value.startswith(("out:", "src:")),
                Target(_atom("action_seq", row), "action_seq", row),
                extra,
            )
        )

    def exact(spelled: list[str], prefix: str, value: str, is_path: bool) -> bool:
        return (
            flag in spelled
            or prefix + value == flag
            or (is_path and flag.startswith(prefix) and _path_matches(value, flag[len(prefix) :]))
        )

    def basename(spelled: list[str], prefix: str, value: str, is_path: bool) -> bool:
        return "/" not in flag and any(t.rsplit("/", 1)[-1] == flag for t in spelled)

    def starts(spelled: list[str], prefix: str, value: str, is_path: bool) -> bool:
        return any(t.startswith(flag) for t in spelled)

    found: list[Target] = []
    for test in (exact, basename, starts):
        for spelled, prefix, value, is_path, target, extra in candidates:
            if test(spelled, prefix, value, is_path):
                found += [target, *extra]
        if found:
            break
    if loose and found:
        found = _one_action_per_token(model, found, wanted or set())
    if not found:
        tokens = sorted({t for c in candidates for t in c[0]})
        close = difflib.get_close_matches(flag, tokens, n=3, cutoff=0.5)
        hint = f"; did you mean {', '.join(close)}?" if close else ""
        where = f" on the command line of {output}" if output else ""
        raise BuildlogError(f"no action_arg spells {flag}{where}{hint}")
    return sorted(set(found), key=lambda t: t.atom)


def _one_action_per_token(model: Model, found: list[Target], wanted: set[str]) -> list[Target]:
    """For a target or source given loosely, one action per distinct token:
    the same flag on every object of a target has the same explanation.
    The target's final actions (whose outputs no other wanted action reads)
    are preferred."""
    consumed = {p for a, p, _ in model.derived_rows("action_in") if a in wanted}

    def key(t: Target) -> tuple[bool, str]:
        return (t.row[0] in consumed, t.row[0])

    token = {"action_arg": lambda r: (r[2], r[3]), "action_seq": lambda r: (r[2],)}
    chosen: dict[tuple, str] = {}
    for t in sorted(found, key=key):
        if t.relation in token:
            chosen.setdefault((t.relation, *token[t.relation](t.row)), t.row[0])
    keep = set(chosen.values())
    return [t for t in found if t.row[0] in keep and _chosen(t, chosen, token)]


def _chosen(t: Target, chosen: dict, token: dict) -> bool:
    if t.relation in token:
        return chosen[(t.relation, *token[t.relation](t.row))] == t.row[0]
    return True


def _spell(prefix: str, value: str) -> list[str]:
    spelled = [prefix + _ninja_path(value)]
    if value.startswith("src:"):
        spelled.append(prefix + value[4:])
    return spelled
