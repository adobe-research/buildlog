# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The buildlog proof protocol (docs/proof-protocol.md), built from Soufflé's
JSON proofs.

Soufflé prints a proof as a tree of strings. `build_proof` parses every
label into typed values, shares repeated subproofs (the result is a DAG
whose nodes are referenced by id), maps rule numbers to layer clauses, and
annotates leaves with their origin: the TOML/.facts line, the probe that
produced a discovery result, the layer file of a program fact, or whatever
an importer recorded in the source map.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..errors import BuildlogError
from ..facts import tuple_key
from .program import ProgramIndex
from .souffle import Model
from .values import (
    ValueSyntaxError,
    explain_text,
    facts_text,
    parse_atom,
    parse_constraint,
    parse_value,
    to_json,
)

FORMAT = "buildlog-proof"
VERSION = 1

# Kinds of nodes.
DERIVED, FACT, NEGATION, CONSTRAINT, TRUNCATED, MISSING = (
    "derived",
    "fact",
    "negation",
    "constraint",
    "truncated",
    "missing",
)

# Origin kinds a leaf can carry. Importers add their own (any other string)
# by writing origin objects into the source map; see ORIGIN_HOOK in the docs.
(
    ORIGIN_SPEC,
    ORIGIN_PACKAGE,
    ORIGIN_PROBE,
    ORIGIN_LAYER,
    ORIGIN_AGGREGATE,
    ORIGIN_MANIFEST,
    ORIGIN_DRIVER,
    ORIGIN_UNKNOWN,
) = (
    "spec",
    "package",
    "probe",
    "layer",
    "aggregate",
    "manifest",
    "driver",
    "unknown",
)
# What the driver wrote into a driver relation (core manifest `driver`), for
# the origin of a row no source file states.
DRIVER_DETAIL = {
    "configure_input": "a file this configure read",
    "configure_arg": "the configure command line",
    "builtin_argv": "how build commands run buildlog -E",
    "config_select": "--config on the configure command line",
    "fact_key": "the first column of a spec or package fact row",
    "author_decision": "buildlog.toml [author_decides]",
}
# The `demand` of a relation a manifest lists under `demand` (probe kinds name the others).
TARGET_DEMAND = "target"
AGGREGATE = re.compile(r"\b(count|sum|min|max|mean)\b[^:]*:")


class Origins:
    """Leaf origins: the source map, probe declarations and layer facts."""

    def __init__(self, model: Model):
        self.model = model
        self.index = model.index
        self.by_key: dict[str, list] = {}
        for key, origins in model.source_map().items():
            try:
                rel, row = json.loads(key)
            except (ValueError, TypeError):
                continue
            self.by_key.setdefault(self._normal_key(rel, row), []).extend(origins)
        self._requests: dict[str, dict[str, tuple]] = {}
        self.package_dirs = sorted(
            ((info["dir"], pkg) for pkg, info in self.index.packages.items()),
            key=lambda item: -len(item[0]),
        )

    def _normal_key(self, rel: str, row: list[str]) -> str:
        info = self.index.relations.get(rel)
        types = [t for _, t in info.attrs] if info else []
        cells = []
        for i, cell in enumerate(row):
            cell = str(cell)
            if i < len(types) and self.index.is_structured(types[i]):
                try:
                    cell = facts_text(parse_value(cell, bare=True))
                except ValueSyntaxError:
                    pass
            cells.append(cell)
        return tuple_key(rel, tuple(cells))

    def _file_origin(self, text: str) -> dict:
        m = re.match(r"(.*):(\d+)\Z", text)
        file, line = (m.group(1), int(m.group(2))) if m else (text, None)
        origin: dict = {"kind": ORIGIN_SPEC, "file": file}
        if line is not None:
            origin["line"] = line
        resolved = str(Path(file).resolve())
        for directory, pkg in self.package_dirs:
            if resolved == directory or resolved.startswith(directory + "/"):
                origin["kind"] = ORIGIN_PACKAGE
                origin["package"] = pkg
                break
        return origin

    def request_args(self, request_rel: str, req_id) -> list | None:
        if request_rel not in self._requests:
            rows = {}
            info = self.index.relations.get(request_rel)
            types = [t for _, t in info.attrs] if info else []
            for row in self.model.derived_rows(request_rel):
                values = []
                for i, cell in enumerate(row):
                    t = types[i] if i < len(types) else "symbol"
                    values.append(typed_cell(self.index, cell, t))
                rows.setdefault(str(row[0]), tuple(values))
            self._requests[request_rel] = rows
        values = self._requests[request_rel].get(str(req_id))
        return list(values[1:]) if values else None

    def _mapped(self, rel: str, values: tuple) -> list[dict]:
        key = tuple_key(rel, tuple(facts_text(v) for v in values))
        out = []
        for o in self.by_key.get(key, []):
            if isinstance(o, dict):
                # Importer hook: origin objects pass through unchanged.
                out.append({"kind": o.get("kind", ORIGIN_UNKNOWN), **o})
            else:
                out.append(self._file_origin(str(o)))
        return _dedupe(out)

    def structural(self, origins: list[dict]) -> bool:
        """A leaf the core layers state as a ground fact: vocabulary, a
        registry row or an algebra's base case (docs/proof-protocol.md,
        "Structural leaves")."""
        return bool(origins) and all(
            o.get("kind") == ORIGIN_LAYER
            and self.index.packages.get(o.get("package"), {}).get("source") == "core"
            for o in origins
        )

    def _manifest_line(self, pkg: str, pattern: str) -> int | None:
        try:
            lines = Path(self.index.packages[pkg]["manifest"]).read_text().splitlines()
        except (KeyError, OSError):
            return None
        for i, line in enumerate(lines, 1):
            if re.match(pattern, line):
                return i
        return None

    def _manifest_origin(self, rel: str, values: tuple) -> dict | None:
        """The manifest line that declares a driver-written row: a provider
        (`[[provider]] name = "cps"`) or a package's target namespace."""
        name = rel.rsplit("__", 1)[-1]
        if name == "package_provider" and values:
            for pkg in self.index.packages:
                text = self._manifest_text(pkg)
                for m in re.finditer(r"^\[\[provider\]\]\s*$", text, re.MULTILINE):
                    tail = text[m.end() :]
                    nm = re.search(r'^name\s*=\s*"([^"]*)"', tail, re.MULTILINE)
                    if nm and nm.group(1) == values[0]:
                        line = text.count("\n", 0, m.end() + nm.start()) + 1
                        return self._manifest_ref(pkg, line, f"provider {values[0]}")
        if name == "target_qualifier" and len(values) == 2 and values[0] in self.index.packages:
            pkg = values[0]
            line = self._manifest_line(pkg, r"target_namespace\s*=") or self._manifest_line(
                pkg, r"name\s*="
            )
            return self._manifest_ref(pkg, line, "target namespace")
        return None

    def _manifest_text(self, pkg: str) -> str:
        try:
            return Path(self.index.packages[pkg]["manifest"]).read_text()
        except (KeyError, OSError):
            return ""

    def _manifest_ref(self, pkg: str, line: int | None, detail: str) -> dict:
        origin = {
            "kind": ORIGIN_MANIFEST,
            "file": self.index.packages[pkg]["manifest"],
            "package": pkg,
            "detail": detail,
        }
        if line:
            origin["line"] = line
        return origin

    def _driver_origin(self, rel: str, values: tuple) -> dict | None:
        drivers = {r for info in self.index.packages.values() for r in info.get("driver", [])}
        raw = self.index.display(rel)[0]
        if raw not in drivers:
            return None
        origin = self._manifest_origin(rel, values)
        if origin:
            return origin
        detail = DRIVER_DETAIL.get(raw, "the driver")
        return {"kind": ORIGIN_DRIVER, "relation": raw, "detail": f"{raw}: {detail}"}

    def of_fact(self, rel: str, values: tuple, parent_rule: dict | None = None) -> list[dict]:
        probe = self.index.probe_of_result.get(rel)
        if probe:
            origin = {
                "kind": ORIGIN_PROBE,
                "probe": probe["kind"],
                "package": probe["package"],
                "entry": probe["entry"],
                "request": values[0] if values else None,
                "request_relation": probe["request"],
            }
            args = self.request_args(probe["request"], values[0]) if values else None
            if args is not None:
                origin["request_args"] = [to_json(a) for a in args]
            # Probes that read files (importers) map result rows to file:line.
            return [*self._mapped(rel, values), origin]
        found = self._mapped(rel, values)
        if found:
            return found
        clause = self.index.fact_clause(rel, values)
        if clause:
            return [
                {
                    "kind": ORIGIN_LAYER,
                    "package": clause.package,
                    "file": clause.source_file,
                    "line": clause.line,
                    "rule": clause.rule_id,
                }
            ]
        rules = [c for c in self.index.clauses_by_head.get(rel, []) if not c.is_fact]
        if rules:
            # Derived, but the engine gives no subproof (aggregate results).
            aggregate = [c for c in rules if AGGREGATE.search(c.body)]
            clause = (aggregate or rules)[0]
            return [
                {
                    "kind": ORIGIN_AGGREGATE if aggregate else ORIGIN_LAYER,
                    "package": clause.package,
                    "file": clause.source_file,
                    "line": clause.line,
                    "rule": clause.rule_id,
                }
            ]
        driven = self._driver_origin(rel, values)
        if driven:
            return [driven]
        if self.index.is_synthetic(rel) and parent_rule:
            # An aggregate's result: Soufflé does not justify the counted tuples.
            origin = {"kind": ORIGIN_AGGREGATE, "detail": "aggregate result, not justified further"}
            for key in ("package", "file", "line"):
                if key in parent_rule:
                    origin[key] = parent_rule[key]
            return [origin]
        why = (
            "an input row with no source-map entry"
            if rel in self.index.inputs
            else "no layer clause states it and the engine gives no subproof"
        )
        return [{"kind": ORIGIN_UNKNOWN, "detail": why}]


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def typed_cell(index: ProgramIndex, cell: str, t: str):
    if index.is_structured(t):
        try:
            return parse_value(cell, bare=True)
        except ValueSyntaxError:
            return cell
    base = t
    seen = set()
    while base in index.types and base not in seen:
        seen.add(base)
        base = index.types[base].base
    if base in ("number", "unsigned"):
        try:
            return int(cell)
        except ValueError:
            return cell
    if base == "float":
        try:
            return float(cell)
        except ValueError:
            return cell
    return cell


class ProofBuilder:
    """Accumulates one DAG over several Soufflé proofs (multi-root queries)."""

    def __init__(self, model: Model):
        self.model = model
        self.index = model.index
        self.origins = Origins(model)
        self.nodes: dict[str, dict] = {}
        self.by_label: dict[str, str] = {}
        self.rules: dict[str, dict] = {}
        self.relations: dict[str, dict] = {}
        self.roots: list[str] = []
        self.queries: list[str] = []
        self.raw_bytes = 0
        self.raw_nodes = 0

    # --- names ---------------------------------------------------------

    def branch_info(self, name: str) -> tuple[str, str | None]:
        return self.index.display(name)

    def rename(self, name: str) -> str:
        return self.index.display(name)[0]

    def display_atom(self, rel: str, values: tuple, negated: bool = False) -> str:
        name = self.index.display(rel)[0]
        args = ", ".join(explain_text(v, self.rename) for v in values)
        return f"{'!' if negated else ''}{name}({args})"

    def relation_entry(self, rel: str) -> None:
        if rel in self.relations:
            return
        info = self.index.relations.get(rel)
        display, package = self.index.display(rel)
        entry: dict = {"name": display, "package": package}
        if info:
            entry["attributes"] = [{"name": n, "type": t} for n, t in info.attrs]
            entry["declared"] = {"file": info.file, "line": info.line}
        entry["input"] = rel in self.index.inputs
        if rel in self.index.probe_of_result:
            entry["probe"] = self.index.probe_of_result[rel]["kind"]
        if rel in self.index.probe_of_demand:
            entry["demand"] = self.index.probe_of_demand[rel]["kind"]
        elif rel in self.index.target_demand:
            entry["demand"] = TARGET_DEMAND
        if self.index.is_synthetic(rel):
            entry["synthetic"] = True
        if rel in self.index.structural:
            entry["structural"] = True
        for key in ("fold", "fold_origins", "elide", "flatten"):
            if rel in getattr(self.index, key):
                entry[key] = True
        self.relations[rel] = entry

    # --- building ------------------------------------------------------

    def add(self, query: str, raw: dict) -> str:
        rules = self.index.match_rules(raw.get("rules", []))
        self.raw_bytes += raw.get("_bytes", 0)
        root = self._node(raw["proof"], rules)
        self.roots.append(root)
        self.queries.append(query)
        return root

    def _new_id(self) -> str:
        return f"n{len(self.nodes)}"

    def _node(self, raw: dict, rules: dict, parent_rule: dict | None = None) -> str:
        self.raw_nodes += 1
        label = raw.get("premises") or raw.get("axiom") or ""
        if label in self.by_label:
            # Every occurrence of a tuple has the same subproof: share it,
            # but still count the raw tree for the size statistics.
            for child in raw.get("children", []):
                self._count(child)
            return self.by_label[label]
        node_id = self._new_id()
        node: dict = {"id": node_id}
        self.nodes[node_id] = node
        self.by_label[label] = node_id
        if "premises" in raw:
            self._atom(node, label, negated_ok=False)
            node["kind"] = DERIVED
            num = int(re.sub(r"\D", "", raw.get("rule-number", "")) or 0)
            rule = rules.get((node.get("relation", ""), num))
            node["rule"] = self._rule(node.get("relation", ""), num, rule)
            node["children"] = [
                self._node(c, rules, self.rules[node["rule"]]) for c in raw.get("children", [])
            ]
            return node_id
        if label in ("Tuple not found", "Relation not found"):
            node.update(kind=MISSING, text=label)
            return node_id
        if label.startswith("subproof "):
            node.update(kind=TRUNCATED, text=label)
            return node_id
        try:
            rel, values, negated = parse_atom(label)
        except ValueSyntaxError:
            node["kind"] = CONSTRAINT
            node["text"] = label
            parsed = parse_constraint(label)
            if parsed:
                op, lhs, rhs = parsed
                node["constraint"] = {
                    "op": op,
                    "lhs": to_json(lhs, self.branch_info),
                    "rhs": to_json(rhs, self.branch_info),
                }
                node["text"] = (
                    f"{explain_text(lhs, self.rename)} {op} {explain_text(rhs, self.rename)}"
                )
            return node_id
        self._fill_atom(node, rel, values, negated)
        if negated:
            node["kind"] = NEGATION
        else:
            node["kind"] = FACT
            node["origins"] = self.origins.of_fact(rel, values, parent_rule)
            if self.origins.structural(node["origins"]):
                node["structural"] = True
        return node_id

    def _count(self, raw: dict) -> None:
        self.raw_nodes += 1
        for child in raw.get("children", []):
            self._count(child)

    def _atom(self, node: dict, label: str, negated_ok: bool) -> None:
        try:
            rel, values, negated = parse_atom(label)
        except ValueSyntaxError:
            node["text"] = label
            return
        self._fill_atom(node, rel, values, negated)

    def _fill_atom(self, node: dict, rel: str, values: tuple, negated: bool) -> None:
        self.relation_entry(rel)
        node["relation"] = rel
        node["tuple"] = [to_json(v, self.branch_info) for v in values]
        node["text"] = self.display_atom(rel, values, negated)
        node["_values"] = values

    def _rule(self, rel: str, num: int, rule: dict | None) -> str:
        if rule and "id" in rule:
            rule_id = rule["id"]
        else:
            rule_id = f"{rel}#R{num}"
        if rule_id not in self.rules:
            entry = {"relation": rel, "number": num}
            if rule:
                entry["engine_text"] = rule["text"]
                for key in ("package", "file", "line", "source"):
                    if key in rule:
                        entry[key] = rule[key]
            self.rules[rule_id] = entry
        return rule_id

    def document(self) -> dict:
        nodes = {}
        for node_id, node in self.nodes.items():
            nodes[node_id] = {k: v for k, v in node.items() if not k.startswith("_")}
        return {
            "format": FORMAT,
            "version": VERSION,
            "queries": self.queries,
            "roots": self.roots,
            "nodes": nodes,
            "rules": dict(sorted(self.rules.items())),
            "relations": dict(sorted(self.relations.items())),
            "stats": {
                "raw_bytes": self.raw_bytes,
                "raw_tree_nodes": self.raw_nodes,
                "dag_nodes": len(self.nodes),
            },
        }


def request_atom(model: Model, kind: str, req_id: str) -> str | None:
    """The engine atom of probe `kind`'s request row with id `req_id`: the
    fact whose proof says why the question was asked (`why --demand`)."""
    index = model.index
    probe = next((p for p in index.probes if p["kind"] == kind), None)
    if probe is None:
        return None
    rel = probe["request"]
    info = index.relations.get(rel)
    types = [t for _, t in info.attrs] if info else []
    for row in model.derived_rows(rel):
        if row and row[0] == req_id:
            values = [
                typed_cell(index, cell, types[i] if i < len(types) else "symbol")
                for i, cell in enumerate(row)
            ]
            return f"{rel}(" + ", ".join(explain_text(v) for v in values) + ")"
    return None


def build_proof(model: Model, queries: list[str], raws: list[dict]) -> dict:
    builder = ProofBuilder(model)
    for query, raw in zip(queries, raws, strict=True):
        builder.add(query, raw)
    return builder.document()


def legend(model: Model) -> dict[str, str]:
    """Value text -> name, from naming relations.

    Packages declare them with the manifest key `legend`. Without a
    declaration, an output relation `(v: T, n: symbol)` with T a record or
    ADT type whose derived rows are injective is taken as one (the core's
    `cfg_name`)."""
    index = model.index
    candidates = list(index.legend)
    if not candidates:
        for rel in sorted(index.outputs):
            info = index.relations.get(rel)
            if not info or len(info.attrs) != 2:
                continue
            if index.is_structured(info.attrs[0][1]) and info.attrs[1][1] == "symbol":
                candidates.append(rel)
    out: dict[str, str] = {}
    for rel in candidates:
        rows = model.derived_rows(rel)
        names: dict[str, str] = {}
        ok = True
        for value, name in rows:
            try:
                text = explain_text(parse_value(value, bare=True))
            except ValueSyntaxError:
                ok = False
                break
            if names.get(text, name) != name:
                ok = False
                break
            names[text] = name
        if ok and len(set(names.values())) == len(names):
            for text, name in names.items():
                out.setdefault(text, name)
    return out


STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')
NAME_TOKEN = re.compile(r"‹([^›]*)›|(?<![\w@])@([A-Za-z0-9_][\w.+-]*)(?![\w.+-]*\()")


def cfg_text(value: str) -> str:
    """The query text of a Cfg read from a derived-relation file."""
    return explain_text(parse_value(value, bare=True))


def config_names(model: Model) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Configuration name -> value text, and declaration (`release`) ->
    configuration names, from the naming relation and `decl_cfg`."""
    by_name = {name: text for text, name in legend(model).items()}
    names = {text: name for name, text in by_name.items()}
    decls: dict[str, set[str]] = {}
    try:
        rows = model.derived_rows(model.index.resolve("decl_cfg"))
    except (KeyError, OSError):
        rows = []
    for decl, value in rows:
        name = names.get(cfg_text(value))
        if name:
            decls.setdefault(decl, set()).add(name)
    return by_name, decls


def match_config(model: Model, name: str) -> set[str]:
    """The configuration names `name` selects: an exact configuration name,
    a declared configuration (`--config release`), or a prefix ending at a
    dot. Empty when none match."""
    by_name, decls = config_names(model)
    if name in by_name:
        return {name}
    if name in decls:
        return set(decls[name])
    return {n for n in by_name if n.startswith(name + ".")}


def expand_names(model: Model, code: str) -> str:
    """Replace the names `why` prints (`‹arm64-macos.release.none›`, or
    `@arm64-macos.release.none`) in the code part of a query with their
    values. A declared configuration (`@release`) or a unique prefix ending
    at a dot (`@arm64-macos.release`) also names one. String literals are
    left alone."""
    if "‹" not in code and "@" not in code:
        return code
    by_name: dict[str, str] | None = None

    def value(m: re.Match) -> str:
        nonlocal by_name
        if by_name is None:
            by_name = config_names(model)[0]
        name = m.group(1) if m.group(1) is not None else m.group(2)
        hits = match_config(model, name)
        if len(hits) == 1:
            return by_name[hits.pop()]
        known = ", ".join(sorted(by_name)) or "none"
        what = "is ambiguous" if hits else "is not a configuration name"
        raise BuildlogError(f"{m.group(0)} {what} (known: {known})")

    out, pos = [], 0
    for m in STRING_LITERAL.finditer(code):
        out += [NAME_TOKEN.sub(value, code[pos : m.start()]), m.group(0)]
        pos = m.end()
    out.append(NAME_TOKEN.sub(value, code[pos:]))
    return "".join(out)
