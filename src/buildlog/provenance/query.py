# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`buildlog query`: an ad-hoc Datalog query over the derived model.

The query is a rule body (`link_file(t, c, f), !imported(t)`). It becomes
one clause of a generated program that includes the configured build's
composed layers and reads the same input facts (spec, package and
probe-result facts of the final discovery round), so no probe runs:

    .decl buildlog_query__result(t: symbol, c: Cfg, f: symbol)
    buildlog_query__result(t, c, f) :- link_file(t, c, f), !imported(t).

Relation and ADT-branch names may be written as their package wrote them;
private names are qualified automatically. Variable types come from the
declarations of the atoms they occur in (through record and ADT literals).
The result is read from Soufflé's `IO=jsonfile` output, which keeps records
and ADT values structured.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..errors import BuildlogError
from .souffle import Model, run_program
from .values import NIL, Adt, Record, explain_text, split_top, to_json

RESULT = "buildlog_query__result"
KEYWORDS = {"nil", "true", "false", "as", "count", "sum", "min", "max", "mean", "range"}
IDENT = re.compile(r"[A-Za-z_][\w.]*")
FUNCTORS = {
    "cat",
    "contains",
    "match",
    "not_contains",
    "not_match",
    "ord",
    "strlen",
    "substr",
    "to_number",
    "to_string",
    "to_float",
    "to_unsigned",
    "autoinc",
    "min",
    "max",
    "range",
    "lor",
    "land",
    "lxor",
    "bnot",
    "lnot",
    "bor",
    "band",
    "bxor",
    "bshl",
    "bshr",
    "bshru",
}


@dataclass
class QueryResult:
    query: str
    variables: list[str]
    types: dict[str, str]
    rows: list[tuple]
    program: str


def _mask_strings(text: str) -> str:
    return re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: " " * len(m.group(0)), text)


def _variables(text: str) -> list[str]:
    masked = _mask_strings(text)
    out: list[str] = []
    for m in IDENT.finditer(masked):
        name = m.group(0)
        start, end = m.start(), m.end()
        if start and masked[start - 1] in "$@\\":
            continue
        if masked[end:].lstrip().startswith("("):
            continue
        if name in KEYWORDS or name == "_":
            continue
        if name not in out:
            out.append(name)
    return out


class _Compiler:
    def __init__(self, model: Model):
        self.model = model
        self.model = model
        self.index = model.index
        self.types: dict[str, str] = {}
        self.branch_names: dict[str, str] = {}
        for td in self.index.types.values():
            for branch in td.branches:
                raw = self.index.display(branch)[0]
                self.branch_names.setdefault(raw, branch)

    def qualify(self, body: str) -> str:
        """Rewrite relation and branch names as written by packages to program names."""
        out, pos = [], 0
        for m in re.finditer(r'"(?:[^"\\]|\\.)*"', body):
            out.append(self._qualify_code(body[pos : m.start()]))
            out.append(m.group(0))
            pos = m.end()
        out.append(self._qualify_code(body[pos:]))
        return "".join(out)

    def _qualify_code(self, code: str) -> str:
        from .proof import expand_names

        code = expand_names(self.model, code)

        def atom(m: re.Match) -> str:
            name = m.group(2)
            if m.group(1) == "$":
                branch = self.branch_names.get(name, name)
                return f"${branch}{m.group(3)}"
            if name in FUNCTORS and name not in self.index.relations:
                return m.group(0)
            try:
                return f"{m.group(1)}{self.index.resolve(name)}{m.group(3)}"
            except KeyError as exc:
                raise BuildlogError(str(exc).strip("'\"")) from exc

        return re.sub(r"(\$?)\b([A-Za-z_][\w.]*)(\s*\()", atom, code)

    def infer(self, term: str, typ: str) -> None:
        term = term.strip()
        if IDENT.fullmatch(term) and term not in KEYWORDS and term != "_":
            self.types.setdefault(term, typ)
            return
        td = self.index.resolve_type(typ)
        if td is None:
            return
        if term.startswith("[") and term.endswith("]") and td.kind == "record":
            for sub, (_, ft) in zip(split_top(term[1:-1]), td.fields, strict=False):
                self.infer(sub, ft)
        elif term.startswith("$") and td.kind == "adt":
            m = re.match(r"\$([A-Za-z_][\w.]*)\s*(?:\((.*)\))?\s*\Z", term, re.DOTALL)
            if m and m.group(1) in td.branches:
                args = split_top(m.group(2) or "")
                for sub, (_, ft) in zip(args, td.branches[m.group(1)], strict=False):
                    self.infer(sub, ft)

    def literals(self, body: str) -> None:
        for lit in split_top(body):
            m = re.match(r"!?\s*([A-Za-z_][\w.]*)\s*\((.*)\)\s*\Z", lit, re.DOTALL)
            if not m or m.group(1) not in self.index.relations:
                continue
            rel = self.index.relations[m.group(1)]
            args = split_top(m.group(2))
            if len(args) != len(rel.attrs):
                raise BuildlogError(
                    f"{rel.display} has arity {len(rel.attrs)}, but {len(args)} argument(s) were given"
                )
            for arg, (_, t) in zip(args, rel.attrs, strict=True):
                self.infer(arg, t)


def compile_query(model: Model, query: str) -> tuple[str, list[str], dict[str, str]]:
    body = query.strip().rstrip(".").strip()
    if not body:
        raise BuildlogError("empty query")
    compiler = _Compiler(model)
    qualified = compiler.qualify(body)
    compiler.literals(qualified)
    variables = [v for v in _variables(qualified) if v not in model.index.relations]
    missing = [v for v in variables if v not in compiler.types]
    if missing:
        raise BuildlogError(
            f"cannot infer the type of {', '.join(missing)}: use each variable in a relation atom"
        )
    program_dir = model.index.program_dir
    includes = [inc for inc in model.index._includes() if inc != "io.dl"]
    lines = [f'#include "{(program_dir / inc).as_posix()}"' for inc in includes]
    io = (program_dir / "io.dl").read_text().splitlines()
    lines += [line for line in io if line.startswith(".input")]
    if variables:
        attrs = ", ".join(f"{v}: {compiler.types[v]}" for v in variables)
        head = f"{RESULT}({', '.join(variables)})"
    else:
        attrs = "ok: number"
        head = f"{RESULT}(1)"
    lines += [
        f".decl {RESULT}({attrs})",
        f"{head} :- {qualified}.",
        f".output {RESULT}(IO=jsonfile)",
    ]
    return "\n".join(lines) + "\n", variables, {v: compiler.types[v] for v in variables}


def _from_json(v):
    """A value of Soufflé's jsonfile output: records are lists, nil is null,
    and ADT values are `{"$Branch": [args...]}`."""
    if v is None:
        return NIL
    if isinstance(v, list):
        return Record(tuple(_from_json(x) for x in v))
    if isinstance(v, dict):
        ((branch, args),) = v.items()
        return Adt(branch.removeprefix("$"), tuple(_from_json(x) for x in args))
    return v


def run_query(model: Model, query: str) -> QueryResult:
    program, variables, types = compile_query(model, query)
    work = run_program(model, program, "query")
    rows = []
    for row in json.loads((work / f"{RESULT}.json").read_text()):
        rows.append(tuple(_from_json(c) for c in row) if variables else ())
    rows.sort(key=lambda r: [explain_text(v) for v in r])
    return QueryResult(query, variables, types, rows, program)


def _cell(model: Model, v, abbrev) -> str:
    if isinstance(v, str):
        return v
    text = explain_text(v, lambda b: model.index.display(b)[0])
    named = abbrev(explain_text(v))
    return named if named.startswith("‹") and named.endswith("›") else text


def render_table(model: Model, result: QueryResult, legend: dict[str, str] | None = None) -> str:
    """A variable-binding table; structured values named by `legend` are abbreviated."""
    from .render import _Abbrev

    if not result.variables:
        return ("true." if result.rows else "false.") + "\n"
    abbrev = _Abbrev(legend)
    header = result.variables
    body = [[_cell(model, v, abbrev) for v in row] for row in result.rows]
    widths = [
        max(len(h), *(len(r[i]) for r in body)) if body else len(h) for i, h in enumerate(header)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for r in body:
        lines.append("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())
    lines.append(f"({len(body)} row{'s' if len(body) != 1 else ''})")
    if abbrev.used:
        lines.append("")
        lines += [f"‹{n}› = {v}" for n, v in sorted(abbrev.used.items())]
    return "\n".join(lines) + "\n"


def result_json(model: Model, result: QueryResult) -> dict:
    def branch_info(name):
        return model.index.display(name)

    return {
        "format": "buildlog-query",
        "version": 1,
        "query": result.query,
        "variables": [{"name": v, "type": result.types[v]} for v in result.variables],
        "rows": [[to_json(v, branch_info) for v in row] for row in result.rows],
        "count": len(result.rows),
    }
