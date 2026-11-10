# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Renderers for protocol documents: an indented text tree and static HTML."""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

from .proof import CONSTRAINT, FACT, MISSING, NEGATION, TRUNCATED
from .shape import AUTHORED, _id_key


def display_path(file: str, base: Path | None = None) -> str:
    base = base or Path.cwd()
    try:
        rel = os.path.relpath(file, base)
    except ValueError:
        return file
    return file if rel.startswith("../..") else rel


def origin_text(origin: dict, base: Path | None = None) -> str:
    kind = origin.get("kind", "unknown")
    if kind == "probe":
        args = origin.get("request_args")
        text = f"probe {origin.get('probe')} request {json.dumps(origin.get('request'))}"
        if args:
            text += "(" + ", ".join(json.dumps(a) for a in args) + ")"
        return text
    if origin.get("file"):
        where = display_path(origin["file"], base)
        if origin.get("line"):
            where += f":{origin['line']}"
        if kind == "package":
            pkg = origin.get("package")
            return where if pkg == "project" else f"{where} (package {pkg})"
        if kind in ("spec", "layer"):
            return where
        if kind == "aggregate":
            return f"aggregate {where}"
        if kind == "manifest":
            return f"manifest {where} ({origin.get('detail', 'declared')})"
        return f"{kind} {where}"
    detail = origin.get("detail")
    if kind == "driver":
        return f"driver: {detail}" if detail else "driver"
    if kind == "unknown":
        return f"origin not recorded: {detail}" if detail else "origin not recorded"
    return f"{kind} {detail}" if detail else kind


class _Abbrev:
    def __init__(self, legend: dict[str, str] | None):
        self.legend = sorted((legend or {}).items(), key=lambda kv: -len(kv[0]))
        self.used: dict[str, str] = {}

    def __call__(self, text: str) -> str:
        for value, name in self.legend:
            if value in text:
                text = text.replace(value, f"‹{name}›")
                self.used[name] = value
        return text


def _parents(doc: dict) -> dict[str, int]:
    count: dict[str, int] = {}
    for n in doc["nodes"].values():
        if n.get("collapsed"):
            continue
        for c in n.get("children", []):
            count[c] = count.get(c, 0) + 1
    for r in doc["roots"]:
        count[r] = count.get(r, 0) + 1
    return count


def demand_text(doc: dict, node: dict) -> str:
    """The note on a demand node whose subproof (why it was asked, why the
    target is built) was pruned."""
    probe = doc["relations"].get(node.get("relation", ""), {}).get("demand", "?")
    if probe == "target":
        return "exists here because it is built in this configuration; --demand explains why"
    return f"asked: probe {probe} request; --demand explains why"


def _suffix(doc: dict, node: dict, base: Path | None) -> str:
    kind = node.get("kind")
    if node.get("demand_pruned"):
        return f"  [{demand_text(doc, node)}]"
    if kind == FACT:
        origins = node.get("origins", [])
        return "  ← " + "; ".join(origin_text(o, base) for o in origins) if origins else ""
    if "rule" in node:
        more = "; folded" if node.get("folded") else ""
        return f"  [{node['rule']}{more}]"
    return ""


def _via_text(doc: dict, via: list[str]) -> str:
    names = []
    for v in via:
        n = doc["nodes"].get(v, {})
        rel = n.get("relation", "")
        names.append(doc["relations"].get(rel, {}).get("name", rel))
    return "  (via " + " ← ".join(names) + ")"


def render_text(doc: dict, legend: dict[str, str] | None = None, base: Path | None = None) -> str:
    abbrev = _Abbrev(legend)
    nodes = doc["nodes"]
    parents = _parents(doc)
    printed: set[str] = set()
    lines: list[str] = []

    def label(node: dict) -> str:
        text = abbrev(node.get("text", ""))
        if node.get("kind") == MISSING:
            text = f"not derivable: {text}"
        elif node.get("kind") == TRUNCATED:
            text = f"… {text}"
        return text

    def walk(nid: str, indent: int, via: list[str]) -> None:
        node = nodes[nid]
        pad = "  " * indent
        via_s = _via_text(doc, via) if via else ""
        if nid in printed:
            lines.append(f"{pad}↑ {label(node)}  (#{nid}){via_s}")
            return
        printed.add(nid)
        tag = f"#{nid} " if parents.get(nid, 0) > 1 else ""
        more = f"  … {node['hidden_children']} more" if node.get("truncated") else ""
        lines.append(f"{pad}{tag}{label(node)}{_suffix(doc, node, base)}{via_s}{more}")
        at = len(lines) - 1
        vias = node.get("via", {})
        refs: list[str] = []
        rows: dict[tuple, int] = {}  # a leaf's origins -> its line
        for c in node.get("children", []):
            # A folded node lists its authored leaves; one shown already
            # needs no back-reference.
            if node.get("folded") and c in printed:
                continue
            # A premise shown already is cited on this line, by its id.
            if c in printed:
                cv = vias.get(c, [])
                refs.append(f"#{c} {_via_text(doc, cv).strip()}" if cv else f"#{c}")
                continue
            # Leaves one authored row states (a sugar row and what it
            # desugars to) share that row's line.
            child = nodes[c]
            key = tuple(origin_text(o, base) for o in child.get("origins", []))
            if (
                child.get("kind") == FACT
                and key
                and parents.get(c, 0) == 1
                and not vias.get(c)
                and not child.get("children")
            ):
                if key in rows:
                    printed.add(c)
                    rel = child.get("relation", "")
                    name = doc["relations"].get(rel, {}).get("name", rel)
                    lines[rows[key]] += f"  (+ {name}, same row)"
                    continue
                rows[key] = len(lines)
            walk(c, indent + 1, vias.get(c, []))
        if refs:
            lines[at] += "  ↑ " + ", ".join(refs)

    for r in doc["roots"]:
        walk(r, 0, [])
    if abbrev.used and not doc.get("shape", {}).get("fold"):
        lines.append("")
        lines.append("legend:")
        for name, value in sorted(abbrev.used.items()):
            lines.append(f"  ‹{name}› = {value}")
    return "\n".join(lines) + "\n"


def render_leaves(doc: dict, legend: dict[str, str] | None = None, base: Path | None = None) -> str:
    """`--leaves` text: one origin fact per line."""
    abbrev = _Abbrev(legend)
    lines = []
    for r in doc["roots"]:
        root = doc["nodes"][r]
        lines.append(abbrev(root.get("text", "")))
        # As in the tree: leaves one authored row states (a sugar row and
        # what it desugars to) share a line, the first in proof order.
        rows: dict[tuple, list[str]] = {}
        for c in sorted(root.get("children", []), key=_id_key):
            origins = doc["nodes"][c].get("origins", [])
            authored = origins and all(o.get("kind") in AUTHORED and o.get("line") for o in origins)
            key = tuple(origin_text(o, base) for o in origins) if authored else (c,)
            rows.setdefault(key, []).append(c)
        for c in root.get("children", []):
            group = next(g for g in rows.values() if c in g)
            if group[0] != c:
                continue
            node = doc["nodes"][c]
            more = "".join(f"  (+ {_relation_name(doc, o)}, same row)" for o in group[1:])
            lines.append(f"  {abbrev(node.get('text', ''))}{_suffix(doc, node, base)}{more}")
    return "\n".join(lines) + "\n"


def _relation_name(doc: dict, nid: str) -> str:
    rel = doc["nodes"][nid].get("relation", "")
    return doc["relations"].get(rel, {}).get("name", rel)


CSS = """
body { font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 1.5em; }
details { margin-left: 1.2em; }
summary { cursor: pointer; white-space: nowrap; }
.leaf { margin-left: 2.4em; white-space: nowrap; }
.rule, .via, .more, .demand { color: #777; }
.origin a { color: #0a6; }
.neg { color: #b30; }
.cons { color: #555; font-style: italic; }
.ref a { color: #36c; }
.id { color: #aaa; }
h1 { font-size: 15px; }
"""

JS = """
function setAll(open) {
  document.querySelectorAll('details').forEach(d => { d.open = open; });
}
"""


def _file_link(origin: dict, base: Path | None) -> str:
    text = html.escape(origin_text(origin, base))
    if origin.get("file"):
        uri = Path(origin["file"]).resolve().as_uri()
        if origin.get("line"):
            uri += f"#L{origin['line']}"
        return f'<a href="{html.escape(uri)}">{text}</a>'
    return text


def render_html(
    doc: dict, legend: dict[str, str] | None = None, title: str = "buildlog why"
) -> str:
    abbrev = _Abbrev(legend)
    nodes = doc["nodes"]
    printed: set[str] = set()
    rules = doc.get("rules", {})
    out: list[str] = []

    def rule_html(node: dict) -> str:
        rid = node.get("rule")
        if not rid:
            return ""
        rule = rules.get(rid, {})
        text = html.escape(rid)
        if rule.get("file"):
            uri = Path(rule["file"]).resolve().as_uri() + f"#L{rule.get('line', '')}"
            title_attr = html.escape(rule.get("source", rule.get("engine_text", "")))
            text = f'<a href="{html.escape(uri)}" title="{title_attr}">{text}</a>'
        return f' <span class="rule">[{text}]</span>'

    def label_html(node: dict) -> str:
        text = html.escape(abbrev(node.get("text", "")))
        kind = node.get("kind")
        if kind == NEGATION:
            return f'<span class="neg">{text}</span>'
        if kind == CONSTRAINT:
            return f'<span class="cons">{text}</span>'
        return text

    def via_html(via: list[str]) -> str:
        if not via:
            return ""
        return f' <span class="via">{html.escape(_via_text(doc, via).strip())}</span>'

    def walk(nid: str, via: list[str]) -> None:
        node = nodes[nid]
        if nid in printed:
            out.append(
                f'<div class="leaf ref"><a href="#{nid}">↑ {label_html(node)}</a>{via_html(via)}</div>'
            )
            return
        printed.add(nid)
        anchor = f'<span class="id" id="{nid}">{nid}</span> '
        origins = ""
        if node.get("kind") == FACT:
            links = "; ".join(_file_link(o, None) for o in node.get("origins", []))
            origins = f' <span class="origin">← {links}</span>' if links else ""
        more = ""
        if node.get("truncated"):
            more = f' <span class="more">… {node["hidden_children"]} more</span>'
        if node.get("demand_pruned"):
            more += f' <span class="demand">[{html.escape(demand_text(doc, node))}]</span>'
        head = f"{anchor}{label_html(node)}{rule_html(node)}{origins}{via_html(via)}{more}"
        children = node.get("children", [])
        if not children:
            out.append(f'<div class="leaf">{head}</div>')
            return
        out.append(f"<details open><summary>{head}</summary>")
        vias = node.get("via", {})
        for c in children:
            walk(c, vias.get(c, []))
        out.append("</details>")

    for r in doc["roots"]:
        walk(r, [])
    legend_html = ""
    if abbrev.used:
        items = "".join(
            f"<li>‹{html.escape(n)}› = {html.escape(v)}</li>"
            for n, v in sorted(abbrev.used.items())
        )
        legend_html = f"<h1>legend</h1><ul>{items}</ul>"
    queries = "<br>".join(html.escape(q) for q in doc.get("queries", []))
    data = json.dumps(doc, sort_keys=True).replace("</", "<\\/")
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style><script>{JS}</script></head>"
        f"<body><h1>{queries}</h1>"
        '<p><button onclick="setAll(true)">expand all</button> '
        '<button onclick="setAll(false)">collapse all</button></p>'
        + "\n".join(out)
        + legend_html
        + f'<script type="application/json" id="proof">{data}</script>'
        + "</body></html>\n"
    )
