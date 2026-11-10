# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Proof shaping for humans (docs/proof-protocol.md, "Shaping").

Every transformation maps a protocol document to a protocol document with
a subset of the nodes and the original, stable node ids:

- splice: nodes of engine-introduced relations (`+disconnected0`, aggregate
  subclauses) are replaced by their children. Always on unless `raw`.
- hide: positive atoms of relations a package manifest lists as
  `structural`, and leaves the core layers state as ground facts (node
  field `structural`), are dropped with their subproofs (negations stay:
  they are the causes in error proofs). `--structural` shows them.
- demand: a derived node of a probe's request or domain relation is kept
  as one line (the request and its content) and its subproof is pruned.
  That subproof says why the question was asked, not why the answer holds.
  The same goes for a manifest `demand` relation (core's `needed`, why a
  target is built in a configuration) beneath the attribute of an action
  (`action_arg`, `action_seq`, ...): the action's existence is presupposed.
  Roots are never pruned, and nothing is pruned when every root is a
  demand node, which is how `why --demand` explains the requests
  (`asked_requests`, `asked_targets`) in full. `with_target_demand`
  (`--with-demand`) keeps target demand and still prunes probe requests.
- collapse: a derived node with exactly one premise is plumbing. Its fact
  follows from that premise alone, so edges into it go to the end of the
  single-premise chain, and the skipped nodes are listed under `via`.
- focus: `leaves` keeps only the origin facts under each root; `path_to`
  keeps only the nodes on chains that reach matching leaves. Both still
  drop `elide` rows (below): their subproof is a witness for a term the
  proof already names, chosen by the engine, not a reason.
- fold: on by default (`--full` turns it off). Builtin constraints
  (`1 > 0`) and negations of engine-introduced relations are dropped, and
  a derived node of a relation a manifest lists as `fold` (core's
  configuration construction: `config`, `decl_cfg`, ...) is kept as one
  line marked `folded` without its subproof (`fold_origins`: with the
  authored leaves of its subproof, the spec, user and package facts it was
  built from, for relations whose proof concerns one declaration, such as
  `decl_cfg`); one of an `elide` relation (`cfg_name`) is dropped, and a
  `flatten` relation's recursion (`exec_settings` over a settings list) is
  spliced into its first node. A pruned probe request is dropped too: the
  probe's results cite it. Dropped premises still count when collapsing,
  so folding never merges a rule into its neighbour.
- depth: nodes deeper than `depth` edges below a root are not expanded.
"""

from __future__ import annotations

import fnmatch
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from .proof import CONSTRAINT, DERIVED, FACT, NEGATION, TARGET_DEMAND
from .values import explain_text, from_json

# Origins a person wrote: a folded node keeps these leaves.
AUTHORED = ("spec", "package", "user", "driver")


@dataclass
class Shape:
    raw: bool = False
    collapse: bool = True
    hide_structural: bool = True
    prune_demand: bool = True
    # `--with-demand`: keep target demand (why the target is built) while
    # probe requests stay pruned; `--demand` explains those.
    with_target_demand: bool = False
    depth: int | None = None
    leaves: bool = False
    path_to: str | None = None
    fold: bool = True


class ShapeError(ValueError):
    pass


def origin_labels(origin: dict) -> list[str]:
    out = []
    if origin.get("file"):
        file = origin["file"]
        line = origin.get("line")
        suffix = f":{line}" if line else ""
        out += [f"{file}{suffix}", f"{Path(file).name}{suffix}"]
    if origin.get("kind") == "probe":
        out += [
            f"probe:{origin.get('probe')}",
            f"probe:{origin.get('probe')}:{origin.get('request')}",
        ]
    if origin.get("kind"):
        out.append(f"{origin['kind']}:")
    if origin.get("package"):
        out.append(f"package:{origin['package']}")
    return out


def leaf_matches(doc: dict, node: dict, pattern: str) -> bool:
    if not any(ch in pattern for ch in "*?["):
        pattern = f"*{pattern}*"
    rel = node.get("relation", "")
    info = doc["relations"].get(rel, {})
    texts = [node.get("text", ""), rel, info.get("name", "")]
    if info.get("demand"):
        label = "demand:target" if info["demand"] == TARGET_DEMAND else f"probe:{info['demand']}"
        texts += ["demand:", label]
    for origin in node.get("origins", []):
        texts += origin_labels(origin)
    return any(fnmatch.fnmatchcase(t, pattern) for t in texts if t)


def request_matches(kind: str, atom: str, pattern: str) -> bool:
    """`why --demand PATTERN` on a demand atom: its text, its relation and
    `probe:<kind>` (`demand:target` for target demand), matched as
    `--path-to` matches leaves."""
    if not any(ch in pattern for ch in "*?["):
        pattern = f"*{pattern}*"
    label = "demand:target" if kind == TARGET_DEMAND else f"probe:{kind}"
    texts = [atom, atom.split("(", 1)[0], label]
    return any(fnmatch.fnmatchcase(t, pattern) for t in texts)


def asked_requests(shaped: dict) -> list[tuple[str, str]]:
    """(probe kind, request id) of every probe request a shaped proof relies
    on: its pruned request and domain nodes, and the request behind each
    probe result, which a rule need not join with its request."""
    out: list[tuple[str, str]] = []
    for node in shaped["nodes"].values():
        if node.get("collapsed"):
            continue
        info = shaped["relations"].get(node.get("relation", ""), {})
        if node.get("demand_pruned") and node.get("tuple") and info["demand"] != TARGET_DEMAND:
            out.append((info["demand"], str(node["tuple"][0])))
        for origin in node.get("origins", []):
            if origin.get("kind") == "probe" and origin.get("request") is not None:
                out.append((origin["probe"], str(origin["request"])))
    return list(dict.fromkeys(out))


def asked_targets(shaped: dict) -> list[str]:
    """Engine atoms of the target-demand nodes (`needed(t, c)`) that a shaped
    proof pruned: why each target is built where the proof needs it."""
    out = []
    for node in shaped["nodes"].values():
        info = shaped["relations"].get(node.get("relation", ""), {})
        if node.get("demand_pruned") and info.get("demand") == TARGET_DEMAND:
            args = ", ".join(explain_text(from_json(v)) for v in node["tuple"])
            out.append(f"{node['relation']}({args})")
    return list(dict.fromkeys(out))


def _is_attribute(name: str) -> bool:
    """An attribute of an action: L0's `action_<x>` relations. `action` and
    `action_out` are the action's existence, and are not attributes."""
    return name.startswith("action_") and name != "action_out"


def shape(doc: dict, opts: Shape) -> dict:
    nodes = doc["nodes"]
    relations = doc["relations"]
    roots = list(doc["roots"])
    eff_memo: dict[str, list[str]] = {}
    dropped: dict[str, int] = {}  # premises folding dropped, per node
    root_set = set(roots)

    def demand(nid: str) -> bool:
        n = nodes[nid]
        return bool(relations.get(n.get("relation", ""), {}).get("demand"))

    # A question about demand itself (every root a request) prunes nothing.
    prune = opts.prune_demand and not opts.raw and not all(demand(r) for r in roots)

    # Target demand is pruned only beneath attributes of actions: a question
    # about an action's existence (`--output` alone) is answered by it.
    attributes = all(
        _is_attribute(relations.get(nodes[r].get("relation", ""), {}).get("name", ""))
        for r in roots
    )

    def pruned(nid: str) -> bool:
        if not (prune and nid not in root_set and nodes[nid]["kind"] == DERIVED and demand(nid)):
            return False
        kind = relations[nodes[nid]["relation"]]["demand"]
        if kind == TARGET_DEMAND:
            return attributes and not opts.with_target_demand
        return True

    folding = opts.fold and not opts.raw
    # The focused views keep every origin fact: they fold only builtins and
    # `elide` guards, whose subproof is an arbitrary witness for a term the
    # proof already names, not a reason.
    focused = bool(opts.leaves or opts.path_to)

    def folded(nid: str) -> bool:
        n = nodes[nid]
        return (
            folding
            and not focused
            and nid not in root_set
            and n["kind"] == DERIVED
            and any(
                relations.get(n.get("relation", ""), {}).get(k) for k in ("fold", "fold_origins")
            )
        )

    def asked(nid: str) -> bool:
        """A pruned probe request: folded away, since the probe's result
        leaves cite their request."""
        info = relations.get(nodes[nid].get("relation", ""), {})
        return folding and not focused and pruned(nid) and info.get("demand") != TARGET_DEMAND

    def _asked_request(nid: str) -> bool:
        info = relations.get(nodes[nid].get("relation", ""), {})
        return pruned(nid) and info.get("demand") != TARGET_DEMAND

    def _request_id(nid: str):
        t = nodes[nid].get("tuple") or [None]
        return t[0]

    def synthetic(nid: str) -> bool:
        n = nodes[nid]
        return n["kind"] == DERIVED and relations.get(n.get("relation", ""), {}).get("synthetic")

    def folded_away(nid: str) -> bool:
        """A premise folding drops, which still counts as a premise for
        collapsing (so folding never merges a rule into its neighbour)."""
        n = nodes[nid]
        return folding and (
            n["kind"] == CONSTRAINT
            or relations.get(n.get("relation", ""), {}).get("elide", False)
            or asked(nid)
        )

    def engine_negation(nid: str) -> bool:
        n = nodes[nid]
        return (
            folding
            and n["kind"] == NEGATION
            and relations.get(n.get("relation", ""), {}).get("synthetic", False)
        )

    def authored_leaves(nid: str) -> list[str]:
        """The leaves under a folded node that a person wrote (spec, user and
        package facts): what the folded construction was built from."""
        out, seen, stack = [], set(), list(reversed(nodes[nid].get("children", [])))
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            n = nodes[c]
            if n["kind"] == FACT:
                if not hidden(c) and any(o.get("kind") in AUTHORED for o in n.get("origins", [])):
                    out.append(c)
            elif n["kind"] == DERIVED:
                stack.extend(reversed(n.get("children", [])))
        return out

    def hidden(nid: str) -> bool:
        n = nodes[nid]
        return (
            opts.hide_structural
            and n["kind"] in (DERIVED, FACT)
            and (
                relations.get(n.get("relation", ""), {}).get("structural", False)
                or n.get("structural", False)
            )
        )

    def eff(nid: str) -> list[str]:
        if opts.raw:
            return list(nodes[nid].get("children", []))
        if pruned(nid):
            return []
        if nid in eff_memo:
            return eff_memo[nid]
        if folded(nid):
            origins = relations.get(nodes[nid].get("relation", ""), {}).get("fold_origins")
            eff_memo[nid] = authored_leaves(nid) if origins else []
            return eff_memo[nid]
        eff_memo[nid] = []  # cycle guard (proofs are acyclic)
        out: list[str] = []
        rel = nodes[nid].get("relation", "")
        flat = folding and not focused and relations.get(rel, {}).get("flatten", False)
        for c in nodes[nid].get("children", []):
            if synthetic(c) or (flat and nodes[c].get("relation") == rel):
                out += eff(c)
                dropped[nid] = dropped.get(nid, 0) + dropped.get(c, 0)
            elif folded_away(c):
                dropped[nid] = dropped.get(nid, 0) + 1
            elif not (engine_negation(c) or hidden(c)):
                out.append(c)
        eff_memo[nid] = list(dict.fromkeys(out))
        return eff_memo[nid]

    def plumbing(nid: str) -> bool:
        return (
            opts.collapse
            and not opts.raw
            and nid not in root_set
            and nodes[nid]["kind"] == DERIVED
            and not folded(nid)
            and len(eff(nid)) == 1
            and not dropped.get(nid)
        )

    def resolve(nid: str) -> tuple[str, list[str]]:
        via = []
        while plumbing(nid):
            via.append(nid)
            nid = eff(nid)[0]
        return nid, via

    def reachable(start: list[str]) -> list[str]:
        seen, order, stack = set(), [], list(reversed(start))
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            order.append(nid)
            stack.extend(reversed(eff(nid)))
        return order

    # Shaped edges: child ids and the collapsed chain on each edge.
    edges: dict[str, list[tuple[str, list[str]]]] = {}

    def shaped(nid: str) -> list[tuple[str, list[str]]]:
        if nid not in edges:
            out, seen = [], set()
            for c in eff(nid):
                end, via = resolve(c)
                if end not in seen:
                    seen.add(end)
                    out.append((end, via))
            edges[nid] = out
        return edges[nid]

    if opts.leaves:
        for r in roots:
            leaves = [
                n for n in reachable([r]) if (nodes[n]["kind"] == FACT or pruned(n)) and n != r
            ]
            # As in the tree, a pruned probe request whose result is listed
            # is dropped: the result's origin cites it.
            cited = {
                o.get("request")
                for n in leaves
                for o in nodes[n].get("origins", [])
                if o.get("kind") == "probe"
            }
            leaves = [n for n in leaves if not (_asked_request(n) and _request_id(n) in cited)]
            leaves.sort(key=lambda n: nodes[n].get("text", ""))
            edges[r] = [(n, []) for n in leaves]
        for r in roots:
            for n, _ in edges[r]:
                edges.setdefault(n, [])

    keep: set[str] | None = None
    if opts.path_to:
        graph_nodes = _closure(roots, shaped)
        matches = {
            n
            for n in graph_nodes
            if (nodes[n]["kind"] in (FACT, NEGATION) or pruned(n))
            and not shaped(n)
            and leaf_matches(doc, nodes[n], opts.path_to)
        }
        if not matches:
            raise ShapeError(f"no leaf matches {opts.path_to!r}")
        keep = set()
        # A node is kept if some shaped path from it reaches a match.
        memo: dict[str, bool] = {}

        def reaches(n: str) -> bool:
            if n in memo:
                return memo[n]
            memo[n] = False
            ok = n in matches
            for c, _ in shaped(n):
                ok = reaches(c) or ok
            memo[n] = ok
            return ok

        for r in roots:
            reaches(r)
        keep = {n for n, ok in memo.items() if ok}
        roots = [r for r in roots if r in keep]

    # Breadth-first by depth, so a shared node is expanded at its shallowest use.
    depth_of: dict[str, int] = {}
    queue = deque()
    for r in roots:
        if r not in depth_of:
            depth_of[r] = 0
            queue.append(r)
    out_nodes: dict[str, dict] = {}
    while queue:
        nid = queue.popleft()
        node = {k: v for k, v in nodes[nid].items() if k != "children"}
        if pruned(nid):
            node["demand_pruned"] = True
        elif folded(nid) and nodes[nid].get("children"):
            node["folded"] = True
        children = [(c, via) for c, via in shaped(nid) if keep is None or c in keep]
        if opts.depth is not None and depth_of[nid] >= opts.depth and children:
            node["truncated"] = True
            node["hidden_children"] = len(children)
            children = []
        node["children"] = [c for c, _ in children]
        vias = {c: via for c, via in children if via}
        if vias:
            node["via"] = vias
        out_nodes[nid] = node
        for c, via in children:
            for v in via:
                if v not in out_nodes:
                    collapsed = {k: val for k, val in nodes[v].items() if k != "children"}
                    collapsed["collapsed"] = True
                    out_nodes[v] = collapsed
            if c not in depth_of:
                depth_of[c] = depth_of[nid] + 1
                queue.append(c)
    ordered = {nid: out_nodes[nid] for nid in sorted(out_nodes, key=_id_key)}
    used_rules = {n["rule"] for n in ordered.values() if "rule" in n}
    used_rels = {n["relation"] for n in ordered.values() if "relation" in n}
    result = dict(doc)
    result.update(
        roots=roots,
        nodes=ordered,
        rules={k: v for k, v in doc["rules"].items() if k in used_rules},
        relations={k: v for k, v in doc["relations"].items() if k in used_rels},
        shape=asdict(opts),
    )
    stats = dict(doc.get("stats", {}))
    stats["shaped_nodes"] = sum(1 for n in ordered.values() if not n.get("collapsed"))
    result["stats"] = stats
    return result


def _closure(roots: list[str], shaped) -> set[str]:
    seen, stack = set(), list(roots)
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(c for c, _ in shaped(n))
    return seen


def _id_key(nid: str) -> tuple[int, str]:
    digits = nid.lstrip("n")
    return (int(digits), nid) if digits.isdigit() else (1 << 30, nid)
