# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Static pruning of the composed program for one project.

Given the relations that can hold facts, a clause whose positive body atom is
over a relation that can never be non-empty cannot fire. Dropping such
clauses (and then the declarations and inputs nothing reads) does not change
the model of any relation: a pruned program derives the same rows.

What can be non-empty is a least fixpoint:

- the input relations with facts (the project's static facts and the
  driver's rows, known before the first round) and the results of probe
  kinds whose rules may read them without the request (`unguarded`);
- the results of the other probe kinds, once their request relation can be
  non-empty (every rule reads such a result joined with its request, so
  without a request the rows are inert);
- the heads of every clause that can fire: all its positive atoms outside
  aggregates are over relations that can be non-empty. Negated atoms and
  atoms inside aggregates never block a clause (a negation holds over an
  empty relation, `count` of one is 0). A clause with a disjunction or a
  `choice-domain` always counts as firing.

Every output relation stays declared and output (the driver reads them all,
an empty one as an empty file), as does every input that can be non-empty.
Anything this does not model (components, pragmas, functors, several
relations in one declaration) makes the program unprunable: None.

The program is the C-preprocessed text of build.dl, so the pruned text has
no includes or macros. The row order of an output may differ from the
unpruned run's (symbols are numbered by first sight in the program text);
the rows do not.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FORMAT = "1"
IDENT = r"[A-Za-z_][A-Za-z_0-9]*"
_KEPT_DIRECTIVES = (".type", ".input", ".output", ".plan")
_BARE_AGGREGATE = re.compile(r"\b(?:count|sum|min|max|mean)\b[^:;{}]*:\s*[^{\s]")


@dataclass(frozen=True)
class Pruned:
    text: str
    clauses: tuple[int, int]  # before, after
    decls: tuple[int, int]
    inputs: tuple[int, int]


def preprocess(program_dir: Path) -> str | None:
    """build.dl after the C preprocessor (what Soufflé itself runs), or None."""
    cc = shutil.which("cc") or shutil.which("cpp")
    if cc is None:
        return None
    flags = ["-E", "-P", "-x", "c", "-w", "-I", str(program_dir)]
    done = subprocess.run(
        [cc, *flags, str(program_dir / "build.dl")], capture_output=True, text=True, check=False
    )
    return done.stdout if done.returncode == 0 else None


def _strip_strings(s: str) -> str:
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
            i += 1
            out.append('""')
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _split(text: str) -> list[tuple[str, str]] | None:
    """(kind, text) of each statement: decl, dir (.type/.input/.output/.plan)
    or clause. None on a directive this does not model."""
    stmts: list[tuple[str, str]] = []
    lines = text.split("\n")
    i, buf, depth, in_str = 0, [], 0, False
    while i < len(lines):
        line = lines[i]
        if not buf and not in_str and line.lstrip().startswith("."):
            word = line.lstrip().split()[0]
            chunk = line
            if word == ".decl":
                while chunk.count("(") > chunk.count(")"):
                    i += 1
                    chunk += "\n" + lines[i]
                stmts.append(("decl", chunk))
            elif word in _KEPT_DIRECTIVES:
                while i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|"):
                    i += 1
                    chunk += "\n" + lines[i]
                stmts.append(("dir", chunk))
            else:
                return None
            i += 1
            continue
        j = 0
        while j < len(line):
            c = line[j]
            if in_str:
                if c == "\\":
                    j += 1
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "/" and line[j : j + 2] == "//":
                line = line[:j]
                break
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "." and depth == 0 and (j + 1 == len(line) or line[j + 1] in " \t"):
                buf.append(line[: j + 1])
                stmts.append(("clause", "\n".join(buf)))
                buf = []
                line = line[j + 1 :]
                j = -1
            j += 1
        if line.strip() or buf:
            buf.append(line)
        i += 1
    return None if buf and "".join(buf).strip() else stmts


def _decl_name(text: str) -> str | None:
    """The relation a one-relation declaration declares, else None."""
    m = re.match(rf"\.decl\s+({IDENT})\s*\(", text)
    if m is None:
        return None
    depth = 0
    for k in range(m.end() - 1, len(text)):
        depth += (text[k] == "(") - (text[k] == ")")
        if depth == 0:
            rest = re.sub(r"//[^\n]*", "", text[k + 1 :])
            return m.group(1) if re.fullmatch(r"[\sA-Za-z_]*", rest) else None
    return None


def _atoms(body: str, rels: set[str]) -> tuple[list[str], list[str], bool]:
    """(positive atoms outside aggregates, the other relations referenced,
    whether the body has a disjunction (at any depth)."""
    s = _strip_strings(body)
    pos: list[str] = []
    other: list[str] = []
    brace = paren = 0
    disj = False
    i, n = 0, len(s)
    atom = re.compile(rf"({IDENT})\s*\(")
    while i < n:
        c = s[i]
        if c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
        elif c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif c == ";":
            disj = True
        m = atom.match(s, i) if (c.isalpha() or c == "_") else None
        if m and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] in "_$")):
            name = m.group(1)
            if name in rels:
                k = i - 1
                while k >= 0 and s[k] in " \t\n":
                    k -= 1
                negated = k >= 0 and s[k] == "!"
                (other if negated or brace > 0 else pos).append(name)
            i = m.end() - 1
        elif c.isalnum() or c == "_":
            while i + 1 < n and (s[i + 1].isalnum() or s[i + 1] == "_"):
                i += 1
        i += 1
    return pos, other, disj


def _heads(head: str, rels: set[str]) -> list[str]:
    s = _strip_strings(head)
    return [m.group(1) for m in re.finditer(rf"(?<![\w$])({IDENT})\s*\(", s) if m.group(1) in rels]


def prune_text(
    text: str,
    nonempty_inputs: set[str],
    probes: list[tuple[str, tuple[str, ...], bool]],
) -> Pruned | None:
    """Prune preprocessed program text. `nonempty_inputs`: the input relations
    that can have facts; `probes`: (request, results, unguarded) of each kind."""
    stmts = _split(text)
    if stmts is None:
        return None
    decl_names = [_decl_name(t) for k, t in stmts if k == "decl"]
    if any(n is None for n in decl_names):
        return None
    rels = {n for n in decl_names if n}
    inputs: set[str] = set()
    outputs: set[str] = set()
    for k, t in stmts:
        if k == "dir":
            m = re.match(r"\.(input|output)\s+(\S+)", t)
            if m:
                (inputs if m.group(1) == "input" else outputs).add(re.split(r"[(,]", m.group(2))[0])
    if not (inputs | outputs) <= rels:
        return None
    nonempty = {r for r in nonempty_inputs if r in inputs}
    for _req, results, unguarded in probes:
        if unguarded:
            nonempty |= {r for r in results if r in inputs}
    clauses: list[tuple[int, list[str], list[str], list[str], bool]] = []
    for idx, (k, t) in enumerate(stmts):
        if k != "clause":
            continue
        if ":-" not in t:
            clauses.append((idx, _heads(t, rels), [], [], False))
            continue
        head, body = t.split(":-", 1)
        pos, other, disj = _atoms(body, rels)
        # An aggregate over a bare atom (count : r(x)) holds over an empty r.
        bare = _BARE_AGGREGATE.search(_strip_strings(body)) is not None
        clauses.append((idx, _heads(head, rels), pos, other, disj or bare or "choice-domain" in t))
    live: set[int] = set()
    changed = True
    while changed:
        changed = False
        for req, results, unguarded in probes:
            if not unguarded and req in nonempty:
                for r in results:
                    if r in inputs and r not in nonempty:
                        nonempty.add(r)
                        changed = True
        for idx, heads, pos, _other, disj in clauses:
            if idx not in live and (disj or all(a in nonempty for a in pos)):
                live.add(idx)
                nonempty.update(heads)
                changed = True
    referenced = set(outputs)
    for idx, heads, pos, other, _ in clauses:
        if idx in live:
            referenced |= {*heads, *pos, *other}
    kept_inputs = inputs & nonempty
    referenced |= kept_inputs
    kept: list[str] = []
    n_in = n_out = n_decl = 0
    for idx, (k, t) in enumerate(stmts):
        if k == "clause":
            if idx in live:
                n_out += 1
                kept.append(t)
        elif k == "decl":
            if _decl_name(t) in referenced:
                n_decl += 1
                kept.append(t)
        else:
            m = re.match(r"\.input\s+(\S+)", t)
            if m and re.split(r"[(,]", m.group(1))[0] not in nonempty:
                continue
            if t.lstrip().startswith(".plan") and idx - 1 not in live:
                continue
            kept.append(t)
    n_in = sum(1 for k, _ in stmts if k == "clause")
    return Pruned(
        "\n".join(kept) + "\n",
        (n_in, n_out),
        (len(rels), n_decl),
        (len(inputs), len(kept_inputs)),
    )


def prune_program(
    program_dir: Path,
    nonempty_inputs: set[str],
    probes: list[tuple[str, tuple[str, ...], bool]],
) -> Pruned | None:
    text = preprocess(program_dir)
    return None if text is None else prune_text(text, nonempty_inputs, probes)
