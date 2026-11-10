# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`buildlog why-not`: why a tuple is *not* derived, via `explainnegation`.

Soufflé's `explainnegation` explains one rule at a time, one level deep: it
asks which rule should have derived the tuple, then asks for a value of every
variable not fixed by the head (including anonymous `_` ones), and reports
each body literal as holding or not under that assignment. Its prompts are
only printed on a terminal, so the session runs on a pty (`Repl`).

The driver answers the prompts itself:

- Rules whose head constants cannot match the tuple are skipped.
- Head variables are bound from the tuple (`(20+r)` is solved for `r`).
- The remaining variables get a *witness*: the first answer (in sorted
  order) of the longest prefix of the rule body that has one, computed with
  an ad-hoc query over the derived model. The first literal that fails under
  that witness is the likely reason the rule did not fire.
- `--bind var=value` overrides any variable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..errors import BuildlogError
from .query import run_query
from .souffle import Model, Repl
from .values import (
    Adt,
    Record,
    ValueSyntaxError,
    explain_text,
    parse_atom,
    parse_value,
    split_top,
    to_json,
)

FORMAT = "buildlog-why-not"
VERSION = 1
VAR = re.compile(r"[+@]?[A-Za-z_][\w]*")
OFFSET = re.compile(
    r"\(\s*(?:(-?\d+)\s*\+\s*([+@]?[A-Za-z_]\w*)|([+@]?[A-Za-z_]\w*)\s*\+\s*(-?\d+))\s*\)"
)


@dataclass
class EngineRule:
    number: int
    text: str
    head: str
    head_args: list[str]
    body: list[str]
    bindings: dict[str, object] = field(default_factory=dict)


def _parse_rules(text: str) -> list[EngineRule]:
    rules = []
    for m in re.finditer(r"^(\d+): (.*?)(?=\n\n|\Z)", text, re.MULTILINE | re.DOTALL):
        rule_text = m.group(2).strip()
        head_text, _, body_text = rule_text.partition(":-")
        hm = re.match(r"\s*([+@\w.]+)\s*\((.*)\)\s*\Z", head_text.strip(), re.DOTALL)
        if not hm:
            continue
        body = split_top(body_text.strip().rstrip("."))
        rules.append(
            EngineRule(
                int(m.group(1)),
                " ".join(rule_text.split()),
                hm.group(1),
                split_top(hm.group(2)),
                [" ".join(b.split()) for b in body if b],
            )
        )
    return rules


def _is_var(term: str) -> bool:
    return bool(VAR.fullmatch(term)) and term not in ("nil",)


def _unify_head(rule: EngineRule, values: tuple) -> str | None:
    """Bind head variables from the tuple; a reason string if the head cannot match."""
    for term, value in zip(rule.head_args, values, strict=False):
        term = term.strip()
        if _is_var(term):
            if term in rule.bindings and rule.bindings[term] != value:
                return f"{term} would need two values"
            rule.bindings[term] = value
            continue
        m = OFFSET.fullmatch(term)
        if m and isinstance(value, int):
            const, var = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            rule.bindings.setdefault(var, value - int(const))
            continue
        try:
            const = parse_value(term)
        except ValueSyntaxError:
            continue
        if isinstance(const, Record | Adt):
            continue
        if const != value:
            return f"head argument {term} does not match {explain_text(value)}"
    return None


def _query_name(var: str) -> str:
    if var.startswith("+underscore_"):
        return "anon_" + var.removeprefix("+underscore_")
    if var.startswith("@"):
        return "gen_" + var[1:].split("_")[-1]
    return var


def _rename_vars(text: str, mapping: dict[str, str]) -> str:
    """Replace variables (not relation/functor names, not inside strings)."""
    out, pos = [], 0
    for m in re.finditer(r'"(?:[^"\\]|\\.)*"', text):
        out.append(_rename_code(text[pos : m.start()], mapping))
        out.append(m.group(0))
        pos = m.end()
    out.append(_rename_code(text[pos:], mapping))
    return "".join(out)


def _rename_code(code: str, mapping: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(0)
        start = m.start()
        if start and code[start - 1] == "$":
            return name
        if code[m.end() :].lstrip().startswith("("):
            return name
        return mapping.get(name, name)

    return re.sub(r"[+@]?[A-Za-z_][\w.]*", sub, code)


def _body_vars(literal: str) -> list[str]:
    found: list[str] = []
    masked = re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: " " * len(m.group(0)), literal)
    for m in re.finditer(r"[+@]?[A-Za-z_][\w.]*", masked):
        name, start = m.group(0), m.start()
        if start and masked[start - 1] == "$":
            continue
        if masked[m.end() :].lstrip().startswith("("):
            continue
        if name in ("nil", "count", "sum", "min", "max", "mean", "as") or "." in name:
            continue
        if name not in found:
            found.append(name)
    return found


def _witness(model: Model, rule: EngineRule) -> None:
    """Bind the body's free variables from the longest satisfiable body prefix."""
    usable = [lit for lit in rule.body if "{" not in lit and not lit.startswith("@")]
    free = [v for lit in usable for v in _body_vars(lit) if v not in rule.bindings]
    if not free:
        return
    subst = {v: explain_text(val) for v, val in rule.bindings.items()}
    for k in range(len(usable), 0, -1):
        prefix = usable[:k]
        if not any(v in free for lit in prefix for v in _body_vars(lit)):
            break
        body = ", ".join(_rename_vars(lit, subst) for lit in prefix)
        names = {v: _query_name(v) for lit in prefix for v in _body_vars(lit) if v in free}
        body = _rename_vars(body, names)
        try:
            result = run_query(model, body)
        except BuildlogError:
            continue
        if not result.rows:
            continue
        back = {q: v for v, q in names.items()}
        for name, value in zip(result.variables, result.rows[0], strict=True):
            if name in back:
                rule.bindings.setdefault(back[name], value)
                subst[back[name]] = explain_text(value)
        break
    # Variables no satisfiable prefix binds: any value of the right type will
    # do, since an earlier literal already fails. Take one from its own atom.
    for lit in usable:
        missing = [v for v in _body_vars(lit) if v not in rule.bindings]
        if not missing or lit.startswith("!") or not re.match(r"[\w.]+\s*\(", lit):
            continue
        names = {v: _query_name(v) for v in _body_vars(lit)}
        try:
            result = run_query(model, _rename_vars(lit, names))
        except BuildlogError:
            continue
        if result.rows:
            back = {q: v for v, q in names.items()}
            for name, value in zip(result.variables, result.rows[0], strict=True):
                if back.get(name) in missing:
                    rule.bindings[back[name]] = value


def _parse_bind(model: Model, binds: list[str]) -> dict[str, object]:
    out = {}
    for b in binds:
        name, sep, text = b.partition("=")
        if not sep or not name.strip():
            raise BuildlogError(f"--bind expects var=value, got {b!r}")
        text = text.strip()
        try:
            value = parse_value(text)
        except ValueSyntaxError:
            value = text
        out[name.strip()] = value
    return out


def _literal(model: Model, axiom: str) -> dict:
    text = axiom.rstrip()
    holds = None
    for mark, value in (("✓", True), (" x", False)):
        if text.endswith(mark):
            holds = value
            text = text[: -len(mark)].rstrip()
            break
    entry: dict = {"engine_text": text, "holds": holds}
    try:
        rel, values, negated = parse_atom(text)
    except ValueSyntaxError:
        entry["kind"] = "constraint"
        entry["text"] = text
        return entry

    def rename(b: str) -> str:
        return model.index.display(b)[0]

    def branch_info(b: str):
        return model.index.display(b)

    entry["kind"] = "negation" if negated else "atom"
    entry["relation"] = model.index.display(rel)[0]
    entry["qualified"] = rel
    entry["tuple"] = [to_json(v, branch_info) for v in values]
    args = ", ".join(explain_text(v, rename) for v in values)
    entry["text"] = f"{'!' if negated else ''}{entry['relation']}({args})"
    return entry


def why_not(
    model: Model,
    atom: str,
    rule_numbers: list[int] | None = None,
    binds: list[str] | None = None,
) -> dict:
    from .proof import expand_names

    rel, values, negated = parse_atom(expand_names(model, atom))
    if negated:
        raise BuildlogError("why-not takes a positive atom")
    qualified = model.index.resolve(rel)
    if qualified in model.index.inputs:
        raise BuildlogError(
            f"{rel} is an input relation; no rule derives it (see the source map instead)"
        )
    engine_atom = f"{qualified}({', '.join(explain_text(v) for v in values)})"
    overrides = _parse_bind(model, binds or [])
    doc: dict = {
        "format": FORMAT,
        "version": VERSION,
        "query": atom,
        "atom": engine_atom,
        "holds": False,
        "rules": [],
        "skipped": [],
    }
    if run_query(model, engine_atom).rows:
        doc["holds"] = True
        doc["message"] = f"{atom} holds; use `buildlog why` for its proof"
        return doc
    with Repl(model) as repl:
        repl.command("format json")
        repl.send(f"explainnegation {engine_atom}")
        text, marker = repl.read_until(["Pick a rule number: ", Repl.PROMPT])
        if marker != "Pick a rule number: ":
            message = " ".join(text.split())
            if "tuple exists" in message.lower():
                doc["holds"] = True
                doc["message"] = f"{atom} holds; use `buildlog why` for its proof"
                return doc
            raise BuildlogError(f"souffle explainnegation: {message or 'no rules'}")
        rules = _parse_rules(text)
        repl.send("0")  # leave the rule prompt; each rule gets its own session below
        repl.read_until([Repl.PROMPT], timeout=30)
        engine_rules = [{"rule-number": f"(R{r.number})", "rule": r.text} for r in rules]
        matched = model.index.match_rules(engine_rules)
        for rule in rules:
            if rule_numbers and rule.number not in rule_numbers:
                continue
            info = matched.get((rule.head, rule.number), {"text": rule.text})
            reason = _unify_head(rule, values)
            if reason:
                doc["skipped"].append(
                    {"number": rule.number, "rule": _rule_ref(info), "reason": reason}
                )
                continue
            rule.bindings.update(overrides)
            _witness(model, rule)
            empty = _empty_literal(model, rule)
            if empty is not None:
                doc["rules"].append(_explain_empty(model, rule, info, empty))
                continue
            doc["rules"].append(_explain_rule(model, repl, engine_atom, rule, info))
    if not doc["rules"]:
        raise BuildlogError(f"no rule for {rel} can derive {atom}")
    return doc


def _empty_literal(model: Model, rule: EngineRule) -> int | None:
    """The first positive atom with a variable no witness could bind and no
    tuple under the known bindings: its relation has nothing to offer, so it
    fails whatever the free variables are (and the engine would ask for
    values nothing supplies)."""
    subst = {v: explain_text(val) for v, val in rule.bindings.items()}
    for i, lit in enumerate(rule.body):
        if lit.startswith(("!", "@")) or "{" in lit or not re.match(r"[\w.]+\s*\(", lit):
            continue
        if all(v in rule.bindings for v in _body_vars(lit)):
            continue
        names = {v: _query_name(v) for v in _body_vars(lit) if v not in rule.bindings}
        query = _rename_vars(_rename_vars(lit, subst), names)
        try:
            if not run_query(model, query).rows:
                return i
        except BuildlogError:
            return None
        return None
    return None


def _explain_empty(model: Model, rule: EngineRule, info: dict, index: int) -> dict:
    subst = {v: explain_text(val) for v, val in rule.bindings.items()}
    literals = []
    for i, lit in enumerate(rule.body):
        text = _rename_vars(lit, subst)
        entry = _literal(model, text + (" x" if i == index else ""))
        if entry["kind"] == "constraint" and (m := re.match(r"(!?)([\w.]+)\s*\(", text)):
            # Free variables left in the atom: shown as written, not parsed.
            display = model.index.display(m.group(2))[0]
            rest = re.sub(r"\+underscore_\d+", "_", text[m.end(2) :])
            entry.update(
                kind="negation" if m.group(1) else "atom",
                relation=display,
                qualified=m.group(2),
                text=m.group(1) + display + rest,
            )
        if i == index:
            entry["empty"] = True
        if i != index:
            entry["holds"] = None
        literals.append(entry)
    return {
        "number": rule.number,
        "rule": _rule_ref(info),
        "bindings": {},
        "literals": literals,
        "first_failure": index,
    }


def _rule_ref(info: dict) -> dict:
    out = {"engine_text": info.get("text")}
    for key in ("id", "package", "file", "line", "source"):
        if key in info:
            out[key] = info[key]
    return out


def _explain_rule(model: Model, repl: Repl, atom: str, rule: EngineRule, info: dict) -> dict:
    repl.send(f"explainnegation {atom}")
    repl.read_until(["Pick a rule number: "])
    repl.send(str(rule.number))
    asked: dict[str, object] = {}
    while True:
        text, marker = repl.read_until(["Pick a value for ", Repl.PROMPT], timeout=60)
        if marker != "Pick a value for ":
            break
        name, _ = repl.read_until([": "])
        name = name.strip()
        if name not in rule.bindings and name.startswith("agg_"):
            # The rule text calls an aggregate result `@generator_N`; the prompt `agg_N`.
            alias = "@generator_" + name.removeprefix("agg_")
            if alias in rule.bindings:
                rule.bindings[name] = rule.bindings[alias]
        if name not in rule.bindings:
            raise BuildlogError(
                f"rule {rule.number}: no value for {name}; pass --bind {name}=<value>"
            )
        asked[name] = rule.bindings[name]
        repl.send(explain_text(rule.bindings[name]))
    start = text.find("{")
    if start < 0:
        raise BuildlogError(f"souffle explainnegation: {' '.join(text.split())}")
    raw = json.loads(text[start:])
    children = raw.get("proof", {}).get("children", [])
    literals = [_literal(model, c.get("axiom", "")) for c in children]
    failing = next((i for i, lit in enumerate(literals) if lit["holds"] is False), None)

    def branch_info(b: str):
        return model.index.display(b)

    return {
        "number": rule.number,
        "rule": _rule_ref(info),
        "bindings": {
            _display_var(v): to_json(val, branch_info) for v, val in sorted(asked.items())
        },
        "literals": literals,
        "first_failure": failing,
    }


def _display_var(name: str) -> str:
    return "_" + name.removeprefix("+underscore_") if name.startswith("+underscore_") else name


def render_text(model: Model, doc: dict, verbose: bool = False) -> str:
    from .proof import legend
    from .render import _Abbrev, display_path

    if doc["holds"]:
        return doc["message"] + "\n"
    abbrev = _Abbrev(legend(model))
    lines = [f"why not {doc['query']}?"]
    for r in doc["rules"]:
        ref = r["rule"]
        where = ref.get("id", f"rule {r['number']}")
        loc = f"  ({display_path(ref['file'])}:{ref['line']})" if "file" in ref else ""
        lines.append("")
        lines.append(f"rule {where}{loc}")
        lines.append(f"  {_display_code(model, ref.get('source') or ref.get('engine_text', ''))}")
        if r["bindings"]:
            binds = ", ".join(
                f"{k}={abbrev(_value_text(model, v))}" for k, v in r["bindings"].items()
            )
            lines.append(f"  with {binds}")
        for i, lit in enumerate(r["literals"]):
            mark = {True: "✓", False: "✗", None: "?"}[lit["holds"]]
            note = "   ← first failing literal" if i == r["first_failure"] else ""
            lines.append(f"    {mark} {abbrev(lit['text'])}{note}")
        failing = r["first_failure"]
        if failing is None:
            lines.append("    (every literal holds under this assignment)")
        else:
            lit = r["literals"][failing]
            rel = lit.get("qualified")
            if lit.get("empty"):
                lines.append(f"    {lit['relation']} has no tuple that can match")
            elif lit["kind"] == "atom" and rel in model.index.inputs:
                lines.append(f"    {lit['relation']} is an input relation: no such fact was given")
            elif lit["kind"] == "atom":
                lines.append(f"    next: buildlog why-not '{lit['text']}'")
    skipped = doc["skipped"]
    if skipped:
        lines.append("")
        lines.append(f"{len(skipped)} other rule(s) for this relation cannot match the head")
        if verbose:
            for s in skipped:
                ref = s["rule"]
                lines.append(f"  {ref.get('id', 'rule ' + str(s['number']))}: {s['reason']}")
    if abbrev.used:
        lines.append("")
        lines.append("legend:")
        for name, value in sorted(abbrev.used.items()):
            lines.append(f"  ‹{name}› = {value}")
    return "\n".join(lines) + "\n"


def _display_code(model: Model, code: str) -> str:
    """Rule text with qualified names shown as their package wrote them."""

    def sub(m: re.Match) -> str:
        return model.index.display(m.group(1))[0] + m.group(2)

    return " ".join(re.sub(r"\b([A-Za-z_][\w]*)(\s*\()", sub, code).split())


def _value_text(model: Model, v) -> str:
    from .values import NIL

    def back(j):
        if isinstance(j, dict):
            if "record" in j:
                return Record(tuple(back(x) for x in j["record"]))
            if "nil" in j:
                return NIL
            return Adt(j["adt"], tuple(back(x) for x in j["args"]))
        return j

    return explain_text(back(v))


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"
