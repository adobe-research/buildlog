# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""An index over a configured build's composed program.

It answers what the proof builder needs without re-running the loader:
relation declarations and types, which package owns a (possibly qualified)
name, which layer clause a Soufflé rule number came from, and which probe
produced a result relation. Sources: `<builddir>/program/` (the qualified
layer copies, `io.dl`, `registry.json`) and the `sources.json` sidecar that
`write_sources` adds at configure time (package directories, original layer
paths, manifest `structural`/`legend` lists, probe declarations).
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .values import ValueSyntaxError, explain_text, parse_value, split_top

SOURCES_FILE = "sources.json"
SOURCES_VERSION = 1

NAME = r"[A-Za-z_][\w.]*"
# String literals and comments (masked before structural scans).
_OPAQUE = re.compile(r'"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/', re.DOTALL)
_TOKEN = re.compile(r'"(?:[^"\\\n]|\\.)*"|\+?[A-Za-z_@][\w.@]*|\d+(?:\.\d+)?|:-|!=|<=|>=|[=<>!]')


def write_sources(program_dir: Path, program) -> None:
    """Record where the composed program came from (called by the engine)."""
    packages = {}
    for layer in program.layers:
        try:
            manifest = tomllib.loads(layer.manifest.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            manifest = {}
        packages[layer.name] = {
            "dir": str(layer.manifest.parent.resolve()),
            "manifest": str(layer.manifest.resolve()),
            "source": layer.source,
            "version": layer.version,
            "files": {
                dst.relative_to(program_dir).as_posix(): str(src.resolve())
                for src, dst in zip(layer.files, layer.program_files, strict=True)
            },
            "fact_files": [str(f.resolve()) for f in layer.fact_files],
            "structural": list(manifest.get("structural", [])),
            "fold": list(manifest.get("fold", [])),
            "fold_origins": list(manifest.get("fold_origins", [])),
            "elide": list(manifest.get("elide", [])),
            "flatten": list(manifest.get("flatten", [])),
            "driver": list(manifest.get("driver", [])),
            "demand": list(manifest.get("demand", [])),
            "legend": list(manifest.get("legend", [])),
        }
        if program.modules is not None:
            scoped = program.modules.programs.get(layer.manifest.resolve())
            if scoped is not None:
                packages[layer.name]["files"][scoped.relative_to(program_dir).as_posix()] = str(
                    scoped
                )
    probes = [
        {
            "kind": p.kind,
            "package": p.layer,
            "request": p.request,
            "domain": p.domain,
            "results": list(p.results),
            "entry": p.entry,
        }
        for p in program.probes
    ]
    data = {"version": SOURCES_VERSION, "packages": packages, "probes": probes}
    (program_dir / SOURCES_FILE).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _mask(text: str) -> str:
    """Blank out strings and comments, keeping offsets and newlines."""
    return _OPAQUE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _preprocess(text: str, defines: set[str]) -> str:
    """Blank lines in inactive `#if` branches (the subset our layers use)."""
    out: list[str] = []
    stack: list[tuple[bool, bool]] = []  # (parent active, a branch was taken)
    active = True
    for line in text.split("\n"):
        m = re.match(r"\s*#\s*(ifdef|ifndef|if|elif|else|endif|define)\b\s*(\S*)", line)
        if not m:
            out.append(line if active else " " * len(line))
            continue
        kind, arg = m.groups()
        if kind in ("ifdef", "ifndef", "if"):
            if kind == "ifdef":
                cond = arg in defines
            elif kind == "ifndef":
                cond = arg not in defines
            else:
                cond = arg not in ("0", "")
            stack.append((active, cond))
            active = active and cond
        elif kind in ("elif", "else") and stack:
            parent, taken = stack[-1]
            cond = kind == "else" or arg not in ("0", "")
            active = parent and not taken and cond
            stack[-1] = (parent, taken or active)
        elif kind == "endif" and stack:
            active = stack.pop()[0]
        elif kind == "define" and active:
            defines.add(re.split(r"[(\s]", arg)[0])
            out.append(line)
            continue
        out.append(" " * len(line))
    return "\n".join(out)


@dataclass
class Clause:
    package: str
    program_file: str  # relative to the program directory
    source_file: str  # the original layer file
    line: int
    text: str
    heads: list[str]
    head_args: list[list[str]]
    body: str
    tokens: Counter
    # Per head: that head's tokens and the body's. Soufflé splits a clause
    # with several heads into one rule per head.
    head_tokens: list[Counter] = field(default_factory=list)

    @property
    def is_fact(self) -> bool:
        return not self.body

    @property
    def location(self) -> str:
        return f"{self.source_file}:{self.line}"

    @property
    def rule_id(self) -> str:
        """Stable across machines: package, path inside the package, line."""
        inside = self.program_file.split("/", 1)[-1]
        return f"{self.package}:{inside}:{self.line}"


@dataclass
class TypeDef:
    name: str
    kind: str  # "record", "adt", "subtype", "alias"
    fields: list[tuple[str, str]] = field(default_factory=list)
    branches: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    base: str = ""


@dataclass
class Relation:
    name: str
    package: str | None
    display: str
    attrs: list[tuple[str, str]]
    file: str
    line: int


def _tokens(text: str) -> Counter:
    toks = []
    for t in _TOKEN.findall(text):
        if t.startswith("+underscore") or t == "_":
            continue
        toks.append(t)
    return Counter(toks)


def _attrs(text: str) -> list[tuple[str, str]]:
    out = []
    for part in split_top(text):
        if ":" in part:
            n, t = part.split(":", 1)
            out.append((n.strip(), t.strip()))
    return out


def _statements(code: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) spans of the masked text; kind is 'directive' or 'clause'."""
    spans = []
    i, n = 0, len(code)
    while i < n:
        while i < n and code[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        start = i
        if code[i] == "#" or (code[i] == "." and i + 1 < n and code[i + 1].isalpha()):
            depth = 0
            while i < n:
                ch = code[i]
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                elif ch == "\n" and depth <= 0 and code[i - 1] != "\\":
                    rest = code[i + 1 :].lstrip(" \t")
                    if not rest.startswith("|"):
                        break
                i += 1
            spans.append((start, i, "directive"))
            continue
        depth = 0
        while i < n:
            ch = code[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "." and depth == 0:
                nxt = code[i + 1] if i + 1 < n else " "
                prev = code[i - 1] if i else " "
                if not (prev.isdigit() and nxt.isdigit()) and (nxt.isspace() or i + 1 == n):
                    i += 1
                    break
            i += 1
        spans.append((start, i, "clause"))
    return spans


class ProgramIndex:
    def __init__(self, builddir: Path):
        self.builddir = builddir
        self.program_dir = builddir / "program"
        self.build = self.program_dir / "build.dl"
        registry_path = self.program_dir / "registry.json"
        self.registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
        sources_path = self.program_dir / SOURCES_FILE
        self.sources = json.loads(sources_path.read_text()) if sources_path.exists() else {}
        self.inputs = set(self.registry.get("inputs", []))
        self.outputs = set(self.registry.get("outputs", []))
        # qualified name -> (as written, package)
        self.unqualified: dict[str, tuple[str, str]] = {}
        for pkg, info in self.registry.get("layers", {}).items():
            for raw, q in info.get("qualified", {}).items():
                self.unqualified[q] = (raw, pkg)
        self.packages: dict[str, dict] = self.sources.get("packages", {})
        self.probes: list[dict] = self.sources.get("probes", [])
        self.probe_of_result = {r: p for p in self.probes for r in p["results"]}
        # Demand relations: a probe's request and domain exist only because
        # something asked the question, so every probe declares them.
        self.probe_of_demand = {
            rel: p for p in self.probes for rel in (p["request"], p["domain"]) if rel
        }
        self.relations: dict[str, Relation] = {}
        self.types: dict[str, TypeDef] = {}
        self.clauses: list[Clause] = []
        self.clauses_by_head: dict[str, list[Clause]] = {}
        self._parse()
        self.fact_clauses: dict[tuple[str, tuple[str, ...]], Clause] = {}
        for c in self.clauses:
            if c.is_fact:
                for head, args in zip(c.heads, c.head_args, strict=True):
                    key = self._fact_key(args)
                    if key is not None:
                        self.fact_clauses.setdefault((head, key), c)
        self.structural = {
            self.qualify(pkg, rel)
            for pkg, info in self.packages.items()
            for rel in info.get("structural", [])
        }
        self.fold, self.fold_origins, self.elide, self.flatten = (
            {
                self.qualify(pkg, rel)
                for pkg, info in self.packages.items()
                for rel in info.get(key, [])
            }
            for key in ("fold", "fold_origins", "elide", "flatten")
        )
        # Target demand: relations that record why a target is built in a
        # configuration (manifest `demand`), beside the probes' requests.
        self.target_demand = {
            self.qualify(pkg, rel)
            for pkg, info in self.packages.items()
            for rel in info.get("demand", [])
        }
        self.legend = [
            self.qualify(pkg, rel)
            for pkg, info in self.packages.items()
            for rel in info.get("legend", [])
        ]

    # --- parsing -------------------------------------------------------

    def _includes(self) -> list[str]:
        if not self.build.exists():
            return []
        return re.findall(r'#include\s+"([^"]+)"', self.build.read_text())

    def package_of_file(self, rel: str) -> str | None:
        for pkg, info in self.packages.items():
            if rel in info.get("files", {}):
                return pkg
        first = rel.split("/", 1)[0]
        return first if first in self.registry.get("layers", {}) else None

    def source_of_file(self, rel: str) -> str:
        for info in self.packages.values():
            if rel in info.get("files", {}):
                return info["files"][rel]
        return str(self.program_dir / rel)

    def _parse(self) -> None:
        defines: set[str] = set()
        for rel in self._includes():
            path = self.program_dir / rel
            if rel == "io.dl" or not path.exists():
                continue
            raw = path.read_text()
            text = _preprocess(raw, defines)
            code = _mask(text)
            pkg = self.package_of_file(rel)
            src = self.source_of_file(rel)
            for start, end, kind in _statements(code):
                line = code.count("\n", 0, start) + 1
                stmt_code = code[start:end]
                stmt = text[start:end]
                if kind == "directive":
                    self._directive(stmt_code, stmt, pkg, src, line)
                    continue
                self._clause(stmt_code, stmt, pkg, rel, src, line)

    def _directive(self, code: str, text: str, pkg, src, line) -> None:
        m = re.match(rf"\.decl\s+({NAME})\s*\((.*?)\)", code, re.DOTALL)
        if m:
            name = m.group(1)
            display, owner = self.unqualified.get(name, (name, pkg))
            attrs = _attrs(text[m.start(2) : m.end(2)])
            self.relations.setdefault(name, Relation(name, owner, display, attrs, src, line))
            return
        m = re.match(rf"\.type\s+({NAME})\s*(<:|=)\s*(.*)", code, re.DOTALL)
        if not m:
            return
        name, op, rest_code = m.group(1), m.group(2), m.group(3)
        rest = text[m.start(3) :].strip()
        if op == "<:":
            self.types[name] = TypeDef(name, "subtype", base=rest_code.strip())
        elif rest_code.strip().startswith("["):
            inner = rest[rest.index("[") + 1 : rest.rindex("]")]
            self.types[name] = TypeDef(name, "record", fields=_attrs(inner))
        elif "{" in rest_code:
            branches = {}
            for bm in re.finditer(rf"({NAME})\s*\{{([^}}]*)\}}", rest):
                branches[bm.group(1)] = _attrs(bm.group(2))
            self.types[name] = TypeDef(name, "adt", branches=branches)
        else:
            self.types[name] = TypeDef(name, "alias", base=rest_code.strip().split("|")[0].strip())

    def _clause(self, code: str, text: str, pkg, rel, src, line) -> None:
        body = ""
        head = text
        idx = code.find(":-")
        if idx >= 0:
            head, body = text[:idx], text[idx + 2 :]
        head = head.strip()
        heads, head_args, head_tokens = [], [], []
        body_tokens = _tokens(body)
        for part in split_top(head.rstrip(".").strip()):
            m = re.match(rf"({NAME})\s*\((.*)\)\s*\Z", part, re.DOTALL)
            if m:
                heads.append(m.group(1))
                head_args.append(split_top(m.group(2)))
                head_tokens.append(_tokens(part) + body_tokens)
        if not heads:
            return
        clause = Clause(
            package=pkg or "",
            program_file=rel,
            source_file=src,
            line=line,
            text=" ".join(text.split()),
            heads=heads,
            head_args=head_args,
            body=body.strip().rstrip(".").strip(),
            tokens=_tokens(text),
            head_tokens=head_tokens,
        )
        self.clauses.append(clause)
        for h in dict.fromkeys(heads):
            self.clauses_by_head.setdefault(h, []).append(clause)

    @staticmethod
    def _fact_key(args: list[str]) -> tuple[str, ...] | None:
        try:
            return tuple(explain_text(parse_value(a)) for a in args)
        except ValueSyntaxError:
            return None

    # --- names ---------------------------------------------------------

    def display(self, name: str) -> tuple[str, str | None]:
        """A relation, type or ADT-branch name as its package wrote it."""
        if name in self.unqualified:
            return self.unqualified[name]
        rel = self.relations.get(name)
        return name, rel.package if rel else None

    def qualify(self, package: str, name: str) -> str:
        info = self.registry.get("layers", {}).get(package, {})
        return info.get("qualified", {}).get(name, name)

    def resolve(self, name: str) -> str:
        """Accept a relation as written by its package (`cxx_object`) or qualified."""
        if name in self.relations:
            return name
        matches = sorted(q for q, (raw, _) in self.unqualified.items() if raw == name)
        matches = [m for m in matches if m in self.relations]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise KeyError(f"{name} is ambiguous: {', '.join(matches)}")
        raise KeyError(f"unknown relation {name}")

    def is_synthetic(self, name: str) -> bool:
        """Relations the engine introduced (`+disconnected0`, `@delta`, aggregates)."""
        return name.startswith(("+", "@", "__agg")) or name not in self.relations

    def resolve_type(self, t: str) -> TypeDef | None:
        seen = set()
        while t in self.types and t not in seen:
            seen.add(t)
            td = self.types[t]
            if td.kind in ("record", "adt"):
                return td
            t = td.base
        return None

    def is_structured(self, t: str) -> bool:
        return self.resolve_type(t) is not None

    # --- rules ---------------------------------------------------------

    def match_rules(self, rules: list[dict]) -> dict[tuple[str, int], dict]:
        """Map Soufflé's relation-local rule numbers to layer clauses.

        Soufflé prints rules without locations. Each printed rule is matched
        to the source clause head of the same relation whose tokens (with the
        clause body's) overlap most (Dice coefficient; variables renamed for `_` are ignored), ties
        broken by source order against the rule number.
        """
        out: dict[tuple[str, int], dict] = {}
        for r in rules:
            num = int(re.sub(r"\D", "", r.get("rule-number", "0")) or 0)
            text = r.get("rule", "")
            m = re.match(r"\s*([+@\w.]+)\s*\(", text)
            if not m:
                continue
            head = m.group(1)
            entry = {"number": num, "text": " ".join(text.split())}
            # One candidate per head of the relation in a clause: a clause
            # with several heads is several rules, each its head plus the body.
            candidates = [
                (c, t)
                for c in self.clauses_by_head.get(head, [])
                if not c.is_fact
                for h, t in zip(c.heads, c.head_tokens, strict=True)
                if h == head
            ]
            if candidates:
                toks = _tokens(text)
                total = sum(toks.values())

                def score(item, toks=toks, total=total, num=num):
                    idx, (_, ctoks) = item
                    common = sum((toks & ctoks).values())
                    dice = 2 * common / max(1, total + sum(ctoks.values()))
                    return (-dice, abs(idx - (num - 1)))

                _, (best, _) = min(enumerate(candidates), key=score)
                entry.update(
                    package=best.package,
                    file=best.source_file,
                    line=best.line,
                    source=best.text,
                    id=best.rule_id,
                )
            out[(head, num)] = entry
        return out

    def fact_clause(self, relation: str, values: tuple) -> Clause | None:
        key = tuple(explain_text(v) for v in values)
        return self.fact_clauses.get((relation, key))
