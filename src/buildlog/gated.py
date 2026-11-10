# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Gated recursion: a syntactic condition on the composed program.

Theorem `thm:ptime` needs the program's constructed terms to be polynomially
many and polynomially large ((H) and (C)). The loader checks a sufficient
syntactic condition on every rule of every layer (core, plugins, packages and
the project) and rejects the program otherwise. There is no opt-out. The
condition is a conservative syntactic check, not a general termination proof.

Column graph. A node is a (relation, column). A rule adds an edge from every
positive-body column holding a variable to every head column whose term
mentions that variable (through `v = expr` equalities). The edge is
*constructing* if the head term, or an expression on the way, builds a term
(a record or ADT constructor, a functor, arithmetic, `range`). Negation and
aggregates add no edge: a cycle through either is unstratifiable, which
Soufflé has already rejected. A *recursive construction* is a constructing
edge whose ends lie in one strongly connected component C. Each needs one
certificate:

  A  every variable of the head term is bound outside C (by a positive atom
     whose column is not in C, or by an equality or aggregate over such
     variables): the term is a function of values that already exist.
  F  a functional fold, for a component that is a single column (H, j): one
     rule folds H into itself through a single driver atom H(..., rho, ...);
     everything else the head term uses is a function of the head's other
     columns (a pattern of variables, determined through equalities and
     aggregates, never through a free choice from a relation), the driver's
     other columns are determined too, and a comparison, a constant step or a
     structural descent makes one of those other columns strictly progress.
  T  the head is one of the template engine's relations, defined in the
     core: its size is bounded by the paper's E (see TRUSTED).

In addition `range(a, b)` must have length-bounded arguments (a literal,
`strlen`, a `count`, or a variable bound to one of these), and `autoinc` is
rejected: they are the only generators of numbers that are not copies or
functions of existing values.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# The template engine: the core's own relations that expand a template row
# once per choice of parameter values. Their tuples are counted by the paper's
# E, so edges into them are not checked (certificate T). Trusted only in the
# core layer: no other layer can write these relations (they are private).
TRUSTED = frozenset(
    f"{p}{name}" for p in ("tmpl_", "ptmpl_") for name in ("chk", "hole", "step", "asg", "value")
)

AGG_KINDS = {"count", "sum", "min", "max", "mean"}
CMP_OPS = ("<=", ">=", "!=", "<", ">", "=")
INFIX_WORDS = {"band", "bor", "bxor", "bshl", "bshr", "bshru", "land", "lor", "lxor"}
KNOWN_PREDICATES = {"contains", "match", "soundex"}


# --- lexing -----------------------------------------------------------------

_TOKEN = re.compile(
    r"""\s*(?:
      (?P<str>"(?:[^"\\\n]|\\.)*")
    | (?P<num>0x[0-9a-fA-F]+|\d+(?:\.\d+)?[uf]?)
    | (?P<id>[A-Za-z_?][A-Za-z0-9_?.]*)
    | (?P<op>:-|<=|>=|!=|[()\[\]{},;:!=<>+\-*/%^$@|])
    )""",
    re.VERBOSE,
)


_COMMENT_OR_STRING = re.compile(r'"(?:[^"\\]|\\.?)*"?|//[^\n]*|/\*(?:.*?\*/|.*)', re.DOTALL)
_NOT_NEWLINE = re.compile(r"[^\n]")


def _blank(m: re.Match) -> str:
    s = m.group()
    if s[0] == '"':
        return s
    return "" if s[1] == "/" else _NOT_NEWLINE.sub(" ", s)


@functools.lru_cache(maxsize=256)
def strip_comments(text: str) -> str:
    """The text without `//` comments; a `/* */` comment becomes blanks (its
    newlines kept), so lines and columns do not move. Strings are left as they are."""
    return _COMMENT_OR_STRING.sub(_blank, text)


def drop_preprocessor(text: str) -> str:
    """Blank preprocessor lines; of an #ifdef keep the #else branch (what the
    driver compiles, with no macro defined)."""
    out, skip = [], []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith(("#ifdef", "#ifndef")):
            skip.append(True)
            out.append("")
        elif s.startswith("#else"):
            if skip:
                skip[-1] = not skip[-1]
            out.append("")
        elif s.startswith("#endif"):
            if skip:
                skip.pop()
            out.append("")
        elif s.startswith("#") or any(skip):
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def statements(text: str) -> list[tuple[int, str]]:
    """(first line, text) of each clause; directives are skipped."""
    out, depth, start, i, n = [], 0, 0, 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and c == "." and text[start:i].strip() == "":
            j, d = i, 0
            while j < n:
                if text[j] in "([{":
                    d += 1
                elif text[j] in ")]}":
                    d -= 1
                elif text[j] == "\n" and d == 0:
                    break
                j += 1
            start = i = j
            continue
        elif depth == 0 and c == "." and (i + 1 == n or text[i + 1] in " \t\r\n"):
            body = text[start:i]
            first = len(body) - len(body.lstrip())
            out.append((text.count("\n", 0, start + first) + 1, body.strip()))
            start = i + 1
        i += 1
    return out


def tokens(s: str) -> list[str]:
    out, pos = [], 0
    s = s.rstrip()
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m:
            raise ValueError(f"cannot tokenize {s[pos : pos + 20]!r}")
        out.append(m.group(m.lastgroup))
        pos = m.end()
    return out


# --- terms ------------------------------------------------------------------


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Const:
    text: str  # a number, a string literal, nil or `_`


@dataclass(frozen=True)
class Rec:
    args: tuple


@dataclass(frozen=True)
class Adt:
    name: str
    args: tuple


@dataclass(frozen=True)
class Fun:
    name: str
    args: tuple


@dataclass(frozen=True)
class Bin:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class Neg:
    arg: object


@dataclass(frozen=True)
class Agg:
    kind: str
    target: object  # None for count
    body: tuple  # alternatives, each a Conj


@dataclass
class Atom:
    rel: str
    args: list
    neg: bool = False


@dataclass
class Conj:
    atoms: list = field(default_factory=list)
    eqs: list = field(default_factory=list)  # (left, right) of `=`
    cmps: list = field(default_factory=list)  # (op, left, right), op != "="
    other: list = field(default_factory=list)  # filter predicates, terms


def children(t):
    if isinstance(t, (Rec, Adt, Fun)):
        return t.args
    if isinstance(t, Bin):
        return (t.left, t.right)
    if isinstance(t, Neg):
        return (t.arg,)
    if isinstance(t, Agg):
        return ((t.target,) if t.target is not None else ()) + tuple(
            x for c in t.body for x in conj_terms(c)
        )
    return ()


def conj_terms(c: Conj):
    for a in c.atoms:
        yield from a.args
    for left, right in c.eqs:
        yield left
        yield right
    for _, left, right in c.cmps:
        yield left
        yield right
    yield from c.other


def term_vars(t) -> set[str]:
    if isinstance(t, Var):
        return {t.name}
    out: set[str] = set()
    for c in children(t):
        out |= term_vars(c)
    return out


def builds(t) -> set[str]:
    """Constructors a term applies: records, ADTs, functors, arithmetic."""
    out: set[str] = set()
    if isinstance(t, Rec):
        out.add("record")
    elif isinstance(t, Adt):
        out.add("$" + t.name)
    elif isinstance(t, Fun) and t.name != "as":
        out.add(t.name)
    elif isinstance(t, (Bin, Neg)):
        out.add("arith")
    if isinstance(t, Agg):
        return out
    for c in children(t):
        out |= builds(c)
    return out


def show(t) -> str:
    if isinstance(t, Var):
        return t.name
    if isinstance(t, Const):
        return t.text
    if isinstance(t, Rec):
        return "[" + ", ".join(show(a) for a in t.args) + "]"
    if isinstance(t, Adt):
        return f"${t.name}(" + ", ".join(show(a) for a in t.args) + ")"
    if isinstance(t, Fun):
        return f"{t.name}(" + ", ".join(show(a) for a in t.args) + ")"
    if isinstance(t, Bin):
        return f"{show(t.left)} {t.op} {show(t.right)}"
    if isinstance(t, Neg):
        return f"-{show(t.arg)}"
    if isinstance(t, Agg):
        return f"{t.kind} ..."
    return "?"


class Parser:
    def __init__(self, toks: list[str], rels: set[str]):
        self.t, self.i, self.rels = toks, 0, rels

    def peek(self, k: int = 0) -> str | None:
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def next(self) -> str:
        tok = self.t[self.i]
        self.i += 1
        return tok

    def expect(self, tok: str) -> None:
        if self.peek() != tok:
            raise ValueError(f"expected {tok!r}, found {self.peek()!r}")
        self.i += 1

    # -- bodies ----
    def body(self, stop: str | None = None) -> list[Conj]:
        """A disjunction of conjunctions, as alternatives."""
        alts: list[Conj] = [Conj()]
        out: list[Conj] = []
        while True:
            lit = self.literal()
            alts = [_merge(a, b) for a in alts for b in lit]
            tok = self.peek()
            if tok == ",":
                self.i += 1
            elif tok == ";":
                self.i += 1
                out += alts
                alts = [Conj()]
            else:
                break
        return out + alts

    def literal(self) -> list[Conj]:
        tok = self.peek()
        if tok == "(" and self._group_is_body():
            self.i += 1
            alts = self.body()
            self.expect(")")
            return alts
        if tok == "!":
            self.i += 1
            if self.peek() == "(":
                self.i += 1
                alts = self.body()
                self.expect(")")
                for c in alts:  # a negated group: only filters, no bindings
                    return [Conj(atoms=[Atom(a.rel, a.args, True) for a in c.atoms])]
                return [Conj()]
            atom = self.atom()
            return [Conj(atoms=[Atom(atom.rel, atom.args, True)])]
        if (
            tok
            and re.match(r"[A-Za-z_?]", tok)
            and self.peek(1) == "("
            and (tok in self.rels or (tok in KNOWN_PREDICATES))
        ):
            if tok in self.rels:
                return [Conj(atoms=[self.atom()])]
            self.i += 2
            args = self.args(")")
            return [Conj(other=list(args))]
        left = self.expr()
        op = self.peek()
        if op not in CMP_OPS:
            return [Conj(other=[left])]
        self.i += 1
        right = self.expr()
        c = Conj()
        if op == "=":
            c.eqs.append((left, right))
        else:
            c.cmps.append((op, left, right))
        return [c]

    def _group_is_body(self) -> bool:
        """Whether a parenthesis opens a group of literals, not an expression."""
        depth, j = 0, self.i
        while j < len(self.t):
            tok = self.t[j]
            if tok in "([{":
                depth += 1
            elif tok in ")]}":
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and tok in (",", ";"):
                return True
            j += 1
        nxt = self.t[j + 1] if j + 1 < len(self.t) else None
        return nxt not in (*CMP_OPS, "+", "-", "*", "/", "%", "^")

    def atom(self) -> Atom:
        name = self.next()
        self.expect("(")
        return Atom(name, self.args(")"))

    def args(self, close: str) -> list:
        out = []
        if self.peek() == close:
            self.i += 1
            return out
        while True:
            out.append(self.expr())
            tok = self.next()
            if tok == close:
                return out
            if tok != ",":
                raise ValueError(f"expected ',' or {close!r}, found {tok!r}")

    # -- expressions ----
    def expr(self):
        left = self.mul()
        while self.peek() in ("+", "-"):
            op = self.next()
            left = Bin(op, left, self.mul())
        return left

    def mul(self):
        left = self.unary()
        while self.peek() in ("*", "/", "%", "^") or self.peek() in INFIX_WORDS:
            op = self.next()
            left = Bin(op, left, self.unary())
        return left

    def unary(self):
        if self.peek() == "-":
            self.i += 1
            return Neg(self.unary())
        return self.primary()

    def _is_aggregate(self) -> bool:
        kw = self.peek()
        if kw not in AGG_KINDS:
            return False
        depth, j = 0, self.i + 1
        while j < len(self.t):
            tok = self.t[j]
            if tok in "([{":
                depth += 1
            elif tok in ")]}":
                depth -= 1
                if depth < 0:
                    return False
            elif depth == 0 and tok == ":":
                return True
            elif depth == 0 and tok in (",", ";"):
                return False
            j += 1
        return False

    def primary(self):
        tok = self.next()
        if tok == "(":
            e = self.expr()
            self.expect(")")
            return e
        if tok == "[":
            return Rec(tuple(self.args("]")))
        if tok == "$":
            name = self.next()
            self.expect("(")
            return Adt(name, tuple(self.args(")")))
        if tok == "@":
            name = self.next()
            self.expect("(")
            return Fun("@" + name, tuple(self.args(")")))
        if tok[0] == '"' or tok[0].isdigit():
            return Const(tok)
        if tok in ("nil", "_"):
            return Const(tok)
        if tok in AGG_KINDS and self._is_agg_after(tok):
            return self._aggregate(tok)
        if self.peek() == "(":
            self.i += 1
            args = tuple(self.args(")"))
            return Fun(tok, args) if not tok[0].isupper() else Adt(tok, args)
        return Var(tok)

    def _is_agg_after(self, kw: str) -> bool:
        self.i -= 1
        try:
            return self._is_aggregate()
        finally:
            self.i += 1

    def _aggregate(self, kw: str) -> Agg:
        target = None
        if self.peek() != ":":
            target = self.expr()
        self.expect(":")
        if self.peek() == "{":
            self.i += 1
            alts = self.body()
            self.expect("}")
        else:
            alts = self.literal()
        return Agg(kw, target, tuple(alts))


def _merge(a: Conj, b: Conj) -> Conj:
    return Conj(a.atoms + b.atoms, a.eqs + b.eqs, a.cmps + b.cmps, a.other + b.other)


# --- rules ------------------------------------------------------------------


@dataclass
class Rule:
    where: str  # file:line
    layer: str
    core: bool
    head: Atom
    body: Conj
    alt: int  # which disjunct of the clause
    clause: str  # file:line, shared by the disjuncts and the heads of one clause


def parse_clauses(text: str, rels: set[str], origin: str, layer: str, core: bool) -> list[Rule]:
    rules: list[Rule] = []
    for line, stmt in statements(drop_preprocessor(strip_comments(text))):
        if ":-" not in stmt:
            continue
        head_text, body_text = stmt.split(":-", 1)
        where = f"{origin}:{line}"
        try:
            hp = Parser(tokens(head_text), rels)
            heads = []
            while hp.peek() is not None:
                heads.append(hp.atom())
                if hp.peek() == ",":
                    hp.i += 1
            alts = Parser(tokens(body_text), rels).body()
        except (ValueError, IndexError) as exc:
            raise ValueError(
                f"{where}: cannot read the clause for the recursion check: {exc}"
            ) from exc
        for h in heads:
            for k, body in enumerate(alts):
                rules.append(Rule(where, layer, core, h, body, k, where))
    return rules


# --- the column graph -------------------------------------------------------

# A node is (rule number, head column): a writer's column, so a reader only
# sees the rules whose heads can match the atom it reads.
Node = tuple[int, int]


@dataclass
class Edge:
    src: Node
    dst: Node
    rule: Rule
    col: int  # head column
    label: frozenset
    copy: bool


def _atom_arg_vars(a: Atom, j: int) -> set[str]:
    return term_vars(a.args[j])


def sources(rule: Rule) -> dict[str, tuple[set[tuple[int, int]], set[str]]]:
    """For each variable: the positive-body (atom, column) it is read from,
    and the constructors applied on the way through equalities."""
    src: dict[str, tuple[set[tuple[int, int]], set[str]]] = defaultdict(lambda: (set(), set()))
    for a in rule.body.atoms:
        if a.neg:
            continue
        for j, arg in enumerate(a.args):
            for v in term_vars(arg):
                src[v][0].add((id(a), j))
    flows: list[tuple[set[str], str, set[str]]] = []
    for left, right in rule.body.eqs:
        for target, other in ((left, right), (right, left)):
            if isinstance(target, Var) and not isinstance(other, Agg):
                flows.append((term_vars(other), target.name, builds(other)))
    changed = True
    while changed:
        changed = False
        for from_vars, to, b in flows:
            cols, bs = src[to]
            before = (len(cols), len(bs))
            for u in from_vars - {to}:
                cols |= src[u][0]
                bs |= src[u][1]
            bs |= b
            if (len(cols), len(bs)) != before:
                changed = True
    return src


def _key(t) -> str:
    return show(t)


def _clash(h, a) -> bool:
    """Whether a written term and a read pattern can never be equal."""
    lit = lambda t: isinstance(t, Const) and t.text != "_"
    if lit(h) and lit(a):
        return h.text != a.text
    if isinstance(h, Adt) and isinstance(a, Adt):
        return h.name != a.name or len(h.args) != len(a.args)
    return (lit(h) and isinstance(a, (Rec, Adt))) or (lit(a) and isinstance(h, (Rec, Adt)))


def compatible(head: Atom, atom: Atom) -> bool:
    return len(head.args) == len(atom.args) and not any(
        _clash(h, a) for h, a in zip(head.args, atom.args, strict=True)
    )


def writers_of(rules: list[Rule]) -> dict[int, list[int]]:
    """For each positive body atom (by id): the rules whose heads can match it."""
    by_rel: dict[str, list[int]] = defaultdict(list)
    for n, r in enumerate(rules):
        by_rel[r.head.rel].append(n)
    out: dict[int, list[int]] = {}
    for r in rules:
        for a in r.body.atoms:
            if not a.neg:
                out[id(a)] = [w for w in by_rel.get(a.rel, ()) if compatible(rules[w].head, a)]
    return out


def column_edges(rules: list[Rule], writers: dict[int, list[int]]) -> list[Edge]:
    edges: list[Edge] = []
    for n, r in enumerate(rules):
        src = sources(r)
        matched = {_key(arg) for a in r.body.atoms if not a.neg for arg in a.args}
        for j, t in enumerate(r.head.args):
            copy = _key(t) in matched
            tb = builds(t)
            for v in term_vars(t):
                cols, bs = src.get(v, (set(), set()))
                for atom_id, col in cols:
                    label = frozenset() if copy else frozenset(tb | bs)
                    for w in writers[atom_id]:
                        edges.append(Edge((w, col), (n, j), r, j, label, copy))
    return edges


def sccs(nodes, succ: dict) -> list[set]:
    index: dict = {}
    low: dict = {}
    on: set = set()
    stack: list = []
    out: list[set] = []
    counter = 0
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(sorted(succ.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(succ.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = set()
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.add(w)
                    if w == v:
                        break
                out.append(comp)
    return out


# --- certificates -----------------------------------------------------------


def _outer_vars(rule: Rule, in_comp) -> set[str]:
    """Variables bound outside the component: by a positive atom column not
    in it, by an aggregate, or by an equality over such variables."""
    out: set[str] = set()
    for a in rule.body.atoms:
        if a.neg:
            continue
        for j, arg in enumerate(a.args):
            if not in_comp(a, j):
                out |= term_vars(arg)
    for left, right in rule.body.eqs:
        for target, other in ((left, right), (right, left)):
            if isinstance(target, Var) and isinstance(other, Agg):
                out.add(target.name)
    changed = True
    while changed:
        changed = False
        for left, right in rule.body.eqs:
            for target, other in ((left, right), (right, left)):
                if isinstance(other, Agg):
                    continue
                if isinstance(target, Var) and target.name not in out and term_vars(other) <= out:
                    out.add(target.name)
                    changed = True
    return out


def _is_pattern(t) -> bool:
    """A term whose value determines its variables: variables and constants
    under record and ADT constructors, or `x +- literal`."""
    if isinstance(t, (Var, Const)):
        return True
    if isinstance(t, (Rec, Adt)):
        return all(_is_pattern(a) for a in t.args)
    return _step(t) is not None


def _step(t):
    """(variable, offset) of `x`, `x + c`, `c + x`, `x - c`, else None."""
    if isinstance(t, Var):
        return (t.name, 0)
    if isinstance(t, Bin) and t.op in "+-":
        if isinstance(t.left, Var) and _is_number(t.right):
            return (t.left.name, int(t.right.text) * (1 if t.op == "+" else -1))
        if t.op == "+" and isinstance(t.right, Var) and _is_number(t.left):
            return (t.right.name, int(t.left.text))
    return None


def _is_number(t) -> bool:
    return isinstance(t, Const) and re.fullmatch(r"\d+", t.text) is not None


def _strict_sub_vars(t) -> set[str]:
    """Variables that are proper subterms of the value of t (under record and
    ADT constructors only)."""
    if not isinstance(t, (Rec, Adt)):
        return set()
    out: set[str] = set()
    for a in t.args:
        if isinstance(a, Var):
            out.add(a.name)
        else:
            out |= _strict_sub_vars(a)
    return out


def _agg_outer(rule: Rule, agg: Agg, others: set[str]) -> set[str]:
    inner: set[str] = set()
    for c in agg.body:
        for t in conj_terms(c):
            inner |= term_vars(t)
    if agg.target is not None:
        inner |= term_vars(agg.target)
    return inner & others


def _rule_vars_without(rule: Rule, skip) -> set[str]:
    out: set[str] = set()
    for t in rule.head.args:
        out |= term_vars(t)
    for t in conj_terms(rule.body):
        if t is not skip:
            out |= term_vars(t) if not isinstance(t, Agg) else set()
    return out


def determined(rule: Rule, start: set[str], fun: dict | None = None) -> set[str]:
    """Variables fixed by `start`: through equalities, destructuring,
    aggregates whose outer variables are fixed, and atoms of functional
    relations whose key arguments are fixed."""
    det = set(start)
    changed = True
    while changed:
        changed = False
        for a in rule.body.atoms:
            keys = (fun or {}).get(a.rel)
            if a.neg or keys is None or len(a.args) <= max(keys, default=-1):
                continue
            if all(term_vars(a.args[c]) <= det for c in keys):
                new = set().union(*(_is_pattern_vars(x) for x in a.args))
                if new - det and all(_is_pattern(x) for x in a.args):
                    det |= new
                    changed = True
        for left, right in rule.body.eqs:
            for target, other in ((left, right), (right, left)):
                if isinstance(other, Agg):
                    if isinstance(target, Var) and target.name not in det:
                        others = _rule_vars_without(rule, other)
                        if _agg_outer(rule, other, others) <= det:
                            det.add(target.name)
                            changed = True
                    continue
                if term_vars(other) <= det:
                    new = _is_pattern_vars(target)
                    if new - det:
                        det |= new
                        changed = True
    return det


def _is_pattern_vars(t) -> set[str]:
    if isinstance(t, Var):
        return {t.name}
    if isinstance(t, (Rec, Adt)):
        out: set[str] = set()
        for a in t.args:
            out |= _is_pattern_vars(a)
        return out
    if isinstance(t, Fun) and t.name == "ord" and len(t.args) == 1 and isinstance(t.args[0], Var):
        return {t.args[0].name}  # ord is injective on symbols
    s = _step(t)
    return {s[0]} if s else set()


def _occurrences(rule: Rule, name: str) -> int:
    n = 0

    def count(t):
        nonlocal n
        if isinstance(t, Var):
            n += t.name == name
        for c in children(t):
            count(c)

    for t in rule.head.args:
        count(t)
    for t in conj_terms(rule.body):
        count(t)
    return n


def _fold_certificate(rule: Rule, j: int, edges_in: list[Edge], reach, fun) -> str | None:
    """Certificate F for the head column j of `rule`, or the reason it fails."""
    h = rule.head
    t = h.args[j]
    drivers = [a for a in rule.body.atoms if not a.neg and a.rel == h.rel]
    if len(drivers) != 1:
        return "the rule needs exactly one body atom of the head's own relation (the driver)"
    d = drivers[0]
    rho = d.args[j]
    if not isinstance(rho, Var):
        return f"the driver's column {j} is not a variable"
    if rho.name not in term_vars(t):
        return "the head term does not use the driver's value"
    if sum(1 for _ in rule.body.atoms if True) and any(
        a is not d and not a.neg and rho.name in set().union(*[term_vars(x) for x in a.args])
        for a in rule.body.atoms
    ):
        return "the driver's value is read by another atom"
    if _count_in(t, rho.name) != 1:
        return "the head term uses the driver's value more than once (the term would double)"
    if _occurrences(rule, rho.name) != 2:
        return "the driver's value is used outside the head term"
    key_vars: set[str] = set()
    for i, a in enumerate(h.args):
        if i == j:
            continue
        if not _is_pattern(a):
            return f"head column {i} is not a pattern of variables (it is {show(a)})"
        key_vars |= term_vars(a)
    det = determined(rule, key_vars, fun)
    free = term_vars(t) - {rho.name} - det
    if free:
        return (
            f"the head term uses {', '.join(sorted(free))}, not determined by the head's other "
            "columns (bind it with an aggregate over them)"
        )
    for i, b in enumerate(d.args):
        if i == j:
            continue
        loose = term_vars(b) - det
        if loose:
            return (
                f"the driver's column {i} uses {', '.join(sorted(loose))}, not determined by "
                "the head's other columns"
            )
    if not any(_progress(rule, d, h, i) for i in range(len(h.args)) if i != j):
        return "no other column strictly progresses between the driver and the head"
    for i in range(len(h.args)):
        if i != j and (h.rel, i) in reach:
            return f"column {i} of {h.rel} is derived from the folded column, so it is not a key"
    return None


def _count_in(t, name: str) -> int:
    if isinstance(t, Var):
        return int(t.name == name)
    return sum(_count_in(c, name) for c in children(t))


def _progress(rule: Rule, d: Atom, h: Atom, i: int) -> bool:
    a, b = h.args[i], d.args[i]
    if isinstance(b, Var) and b.name in _strict_sub_vars(a):
        return True
    la, lb = _linear(rule, a), _linear(rule, b)
    if la and lb and la[0] == lb[0] and la[1] != lb[1]:
        return True
    if isinstance(a, Var) and isinstance(b, Var):
        for op, left, right in rule.body.cmps:
            if op in ("<", ">") and {left, right} == {a, b}:
                return True
    return False


def _linear(rule: Rule, t):
    s = _step(t)
    if s is None:
        return None
    name, off = s
    for _ in range(8):
        for left, right in rule.body.eqs:
            for tgt, other in ((left, right), (right, left)):
                if isinstance(tgt, Var) and tgt.name == name:
                    s2 = _step(other)
                    if s2 and s2[0] != name:
                        name, off = s2[0], off + s2[1]
                        break
            else:
                continue
            break
        else:
            break
    return (name, off)


# --- the check ---------------------------------------------------------------


def _bounded_number(rule: Rule, t, bounded: set[str]) -> bool:
    if isinstance(t, Const):
        return t.text[0].isdigit()
    if isinstance(t, Var):
        return t.name in bounded
    if isinstance(t, Fun) and t.name == "strlen":
        return True
    if isinstance(t, Bin):
        return _bounded_number(rule, t.left, bounded) and _bounded_number(rule, t.right, bounded)
    if isinstance(t, Neg):
        return _bounded_number(rule, t.arg, bounded)
    return False


def _walk(t):
    yield t
    for c in children(t):
        yield from _walk(c)


def range_errors(rule: Rule, trusted: frozenset[str] = frozenset()) -> list[str]:
    """`range(a, b)` generates b - a numbers: the arguments must be lengths."""
    out: list[str] = []
    bounded: set[str] = set()
    for a in rule.body.atoms:  # the template engine's positions are lengths (E)
        if not a.neg and a.rel in trusted:
            for arg in a.args:
                bounded |= term_vars(arg)
    changed = True
    while changed:
        changed = False
        for left, right in rule.body.eqs:
            for tgt, other in ((left, right), (right, left)):
                if not isinstance(tgt, Var) or tgt.name in bounded:
                    continue
                ok = False
                if isinstance(other, Agg):
                    ok = other.kind == "count"
                elif isinstance(other, Fun) and other.name == "range":
                    ok = all(_bounded_number(rule, a, bounded) for a in other.args)
                else:
                    ok = _bounded_number(rule, other, bounded) and not isinstance(other, Var)
                if ok:
                    bounded.add(tgt.name)
                    changed = True
    for t in [*rule.head.args, *conj_terms(rule.body)]:
        for x in _walk(t):
            if (
                isinstance(x, Fun)
                and x.name == "range"
                and not all(_bounded_number(rule, a, bounded) for a in x.args)
            ):
                out.append(
                    f"{rule.where}: {show(x)} ranges over numbers that are not lengths "
                    "(range arguments must be literals, strlen(..), a count, or variables "
                    "bound to one of them)"
                )
            if isinstance(x, Fun) and x.name == "autoinc":
                out.append(f"{rule.where}: autoinc() makes a new number per derivation")
    return out


def functional_relations(rules: list[Rule]) -> dict[str, frozenset[int]]:
    """Relations defined by one rule whose other columns are determined by
    the key columns alone (through equalities and aggregates, not through a
    free choice from a relation): a function of the keys."""
    by_rel: dict[str, list[Rule]] = defaultdict(list)
    for r in rules:
        by_rel[r.head.rel].append(r)
    out: dict[str, frozenset[int]] = {}
    for rel, rs in by_rel.items():
        if len(rs) != 1:
            continue
        r = rs[0]
        if "__" not in rel:  # exported or open relations take rows from elsewhere
            continue
        if not all(isinstance(a, Var) for a in r.head.args):
            continue
        names = [a.name for a in r.head.args]
        if len(set(names)) != len(names):
            continue
        keys = set(range(len(names)))
        for c in reversed(range(len(names))):
            rest = {names[k] for k in keys if k != c}
            if names[c] in determined(r, rest):
                keys.discard(c)
        if len(keys) < len(names):
            out[rel] = frozenset(keys)
    return out


def check_rules(rules: list[Rule], shown=lambda rel: rel) -> list[str]:
    errors: list[str] = []
    trusted = _trusted_names(rules)
    checked = [r for r in rules if not (r.core and r.head.rel in trusted)]
    fun = functional_relations(rules)
    for r in checked:
        errors += range_errors(r, trusted)
    writers = writers_of(checked)
    edges = column_edges(checked, writers)
    succ: dict[Node, set[Node]] = defaultdict(set)
    nodes: set[Node] = set()
    for e in edges:
        succ[e.src].add(e.dst)
        nodes |= {e.src, e.dst}
    comp_of: dict[Node, int] = {}
    comps = sccs(nodes, succ)
    for n, comp in enumerate(comps):
        for node in comp:
            comp_of[node] = n

    def reach_from(start: Node) -> set[Node]:
        seen, todo = set(), [start]
        while todo:
            v = todo.pop()
            for w in succ.get(v, ()):
                if w not in seen:
                    seen.add(w)
                    todo.append(w)
        return {(checked[n].head.rel, c) for n, c in seen}

    inner: dict[tuple, list[Edge]] = defaultdict(list)
    for e in edges:
        if comp_of[e.src] == comp_of[e.dst]:
            inner[(id(e.rule), e.col)].append(e)
    seen_sites: set[tuple] = set()
    for e in edges:
        if comp_of[e.src] != comp_of[e.dst] or not e.label:
            continue
        site = (id(e.rule), e.col)
        if site in seen_sites:
            continue
        seen_sites.add(site)
        comp = comps[comp_of[e.dst]]
        rule = e.rule
        in_comp = lambda a, j, comp=comp: any((w, j) in comp for w in writers[id(a)])
        if _outer_vars(rule, in_comp) >= term_vars(rule.head.args[e.col]):
            continue
        why = None
        if len(comp) == 1:
            into = [
                x
                for x in edges
                if x.dst == e.dst and comp_of[x.src] == comp_of[x.dst] and x.rule is not rule
            ]
            if into:
                why = f"{into[0].rule.where} also folds this column; a fold has one rule"
            else:
                why = _fold_certificate(rule, e.col, inner[site], reach_from(e.dst), fun)
            if why is None:
                continue
        else:
            why = "the term uses a variable bound only inside the cycle"
        names = sorted({f"{shown(checked[n].head.rel)}.{c}" for n, c in comp})
        cyc = ", ".join(names[:8]) + (f", ... ({len(names)} columns)" if len(names) > 8 else "")
        errors.append(
            f"{rule.where}: recursive construction: {show(rule.head.args[e.col])} in "
            f"{shown(rule.head.rel)} column {e.col} is built from a value of the cycle "
            f"{{{cyc}}}; {why}"
        )
    return errors


def _trusted_names(rules: list[Rule]) -> frozenset[str]:
    return frozenset(r.head.rel for r in rules if r.core and _raw_is_trusted(r.head.rel))


def _raw_is_trusted(qualified: str) -> bool:
    return qualified.rsplit("__", 1)[-1] in TRUSTED


def relation_names(files: list[Path]) -> set[str]:
    rels: set[str] = set()
    for f in files:
        text = strip_comments(f.read_text())
        rels |= set(re.findall(r"^\s*\.decl\s+([A-Za-z_][A-Za-z0-9_.]*)", text, re.MULTILINE))
    return rels


# Bump when the cached result's meaning changes beyond this file's own text,
# which the key already covers.
CACHE_FORMAT = "1"


def _cache_key(layers) -> str:
    """What the check reads: each layer's name and tier and the text of its
    qualified files, in order, and the checker's own source. Origins and
    display names only shape error messages, and only passes are cached."""
    h = hashlib.sha256(CACHE_FORMAT.encode())
    h.update(Path(__file__).read_bytes())
    for layer in layers:
        h.update(f"\0{layer.name}\0{layer.source == 'core'}".encode())
        for dst in layer.program_files:
            h.update(b"\0")
            h.update(dst.read_bytes())
    return h.hexdigest()


def check_layers(layers, layer_cache: dict | None = None) -> list[str]:
    """Check the composed program of `layers` (qualified copies of the files).

    A program that passes is remembered by the hash of what the check reads:
    in `layer_cache` (one configure) and in the user's cache directory, so an
    unchanged program is not parsed again."""
    from .souffle_cache import cache_dir

    key = _cache_key(layers)
    if layer_cache is not None and ("gated", key) in layer_cache:
        return []
    base = cache_dir()
    entry = base / "gated" / key[:2] / key if base is not None else None
    if entry is not None and entry.exists():
        if layer_cache is not None:
            layer_cache[("gated", key)] = True
        return []
    errors = _check(layers)
    if not errors:
        if layer_cache is not None:
            layer_cache[("gated", key)] = True
        if entry is not None:
            try:
                entry.parent.mkdir(parents=True, exist_ok=True)
                tmp = entry.with_name(f"{key}.{os.getpid()}.tmp")
                tmp.write_bytes(b"")
                os.replace(tmp, entry)
            except OSError:
                pass
    return errors


def _check(layers) -> list[str]:
    files = [dst for layer in layers for dst in layer.program_files]
    rels = relation_names(files)
    unq = {q: raw for layer in layers for raw, q in layer.qualified.items()}
    rules: list[Rule] = []
    errors: list[str] = []
    for layer in layers:
        for src, dst in zip(layer.files, layer.program_files, strict=False):
            rel = os.path.relpath(src)
            origin = str(src) if rel.startswith("..") else rel
            try:
                rules += parse_clauses(
                    dst.read_text(), rels, origin, layer.name, layer.source == "core"
                )
            except ValueError as exc:
                errors.append(str(exc))
    if errors:
        return errors
    return check_rules(rules, lambda rel: unq.get(rel, rel))
