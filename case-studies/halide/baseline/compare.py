# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compare two normalized build graphs (JSONL from normalize.py).

    uv run python compare.py A.jsonl B.jsonl [--map pathmap.toml] [--details N] [--json out.json]
    uv run python compare.py --self-test cmake-release.jsonl

Records are matched by normalized output path: the record id (its first output) after
applying the path map, falling back to any shared output. For every (phase, kind) the
report counts records that are identical, argv-differing, only in A, and only in B, and
prints a token-level diff for the differing ones.

Argument comparison mode is chosen per kind (override with --mode KIND=set|ordered):

  set      compile, pch. Flags are grouped into units (a flag plus its separate argument,
           e.g. ``-isystem DIR``, ``-D X``, ``-o FILE``; runs of ``-Xclang A -Xclang B``
           are one unit) and compared as multisets, because flag order does not change a
           compile. The exceptions are include search paths (``-I``, ``-isystem``,
           ``-iquote``, ``-F``, ``-idirafter``), whose relative order does matter; their
           sequence is compared in order.
  ordered  everything else: link and archive (input order decides symbol resolution and
           archive layout), custom commands, copies, installs. Tokens are compared as a
           sequence, and the diff is a difflib opcode list.

By default only the record's main step is compared (e.g. the ``c++`` invocation, not the
surrounding ``cd … &&`` or ``cmake -E touch`` bookkeeping), with its stdin/stdout
redirections appended as ``< in`` and ``> out``; --full-argv compares the whole command
line. Excluded records are skipped unless --include-excluded is given.

Path map file (TOML), applied to every path-bearing string of the chosen side::

    [[map]]
    side = "b"                      # "a" or "b" (default "b")
    from = '^\\$BUILD/out/lib/'     # Python regex
    to = '$BUILD/src/'              # replacement (re.sub syntax)

    [modes]                         # optional per-kind overrides
    custom = "set"

    [ignore]                        # optional: argv tokens (regex, full match) to drop
    tokens = ['-fcolor-diagnostics']
    flags = ['-MT']                 # flags dropped together with their separate argument
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import re
import sys
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field

DEFAULT_MODES = {"compile": "set", "pch": "set"}

# Flags whose argument is the following token.
ARG_FLAGS = {
    "-I",
    "-isystem",
    "-iquote",
    "-idirafter",
    "-F",
    "-D",
    "-U",
    "-o",
    "-MF",
    "-MT",
    "-MQ",
    "-MJ",
    "-x",
    "-arch",
    "-target",
    "-include",
    "-imacros",
    "-isysroot",
    "-framework",
    "-L",
    "-l",
    "-Xclang",
    "-Xlinker",
    "-Xpreprocessor",
    "-Xarch_arm64",
    "-Xarch_x86_64",
    "-mllvm",
    "--serialize-diagnostics",
}
# Flags whose (possibly joined) sequence is order-sensitive even in set mode.
ORDERED_PREFIXES = ("-I", "-isystem", "-iquote", "-idirafter", "-F")
CHAIN_FLAGS = {"-Xclang", "-Xlinker", "-Xpreprocessor", "-mllvm"}


# --------------------------------------------------------------------------- loading


def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass
class PathMap:
    rules: list[tuple[str, re.Pattern, str]] = field(default_factory=list)
    modes: dict[str, str] = field(default_factory=dict)
    ignore: list[re.Pattern] = field(default_factory=list)
    ignore_flags: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: str | None) -> PathMap:
        pm = cls()
        if not path:
            return pm
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for m in data.get("map", []):
            side = m.get("side", "b")
            if side not in ("a", "b"):
                raise ValueError(f"path map side must be 'a' or 'b', got {side!r}")
            pm.rules.append((side, re.compile(m["from"]), m["to"]))
        pm.modes.update(data.get("modes", {}))
        pm.ignore = [re.compile(t) for t in data.get("ignore", {}).get("tokens", [])]
        pm.ignore_flags = set(data.get("ignore", {}).get("flags", []))
        return pm

    def apply(self, side: str, s: str) -> str:
        for sd, rx, to in self.rules:
            if sd == side:
                s = rx.sub(to, s)
        return s


def map_record(rec: dict, side: str, pm: PathMap) -> dict:
    """Apply the path map to outputs, inputs and argv tokens of one record (copy)."""
    if not any(sd == side for sd, _, _ in pm.rules):
        return rec
    r = copy.deepcopy(rec)
    f = lambda s: pm.apply(side, s)
    r["id"] = f(r["id"])
    r["outputs"] = [f(o) for o in r.get("outputs", [])]
    if "inputs" in r:
        r["inputs"] = {k: [f(x) for x in v] for k, v in r["inputs"].items()}
    if "argv" in r:
        r["argv"] = [f(t) for t in r["argv"]]
    for st in r.get("steps", []):
        st["argv"] = [f(t) for t in st["argv"]]
        for k in ("cwd", "stdin", "stdout"):
            if k in st:
                st[k] = f(st[k])
    return r


# --------------------------------------------------------------------------- argv


def record_argv(rec: dict, full: bool) -> list[str] | None:
    steps = rec.get("steps")
    if not steps:
        return rec.get("argv")
    if full:
        return rec.get("argv") or [t for st in steps for t in [*st["argv"], "&&"]][:-1]
    main = rec.get("main")
    if main is None:
        return rec.get("argv") or streams(steps[0])
    return streams(steps[main])


def streams(step: dict) -> list[str]:
    """A step's argv, its working directory when it is not the build directory
    (`cd DIR`), and its redirections (`< in`, `> out`): all part of the command."""
    cwd = step.get("cwd")
    return [
        *(["cd", cwd] if cwd not in (None, "$BUILD") else []),
        *step["argv"],
        *(["<", step["stdin"]] if "stdin" in step else []),
        *([">", step["stdout"]] if "stdout" in step else []),
    ]


def units(tokens: list[str]) -> list[tuple[str, ...]]:
    """Group tokens into flag units for set-like comparison."""
    out: list[tuple[str, ...]] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in CHAIN_FLAGS and i + 1 < len(tokens):
            u = [t, tokens[i + 1]]
            i += 2
            while i + 1 < len(tokens) and tokens[i] == t:
                u += [t, tokens[i + 1]]
                i += 2
            out.append(tuple(u))
        elif t in ARG_FLAGS and i + 1 < len(tokens):
            out.append((t, tokens[i + 1]))
            i += 2
        else:
            out.append((t,))
            i += 1
    return out


def _is_ordered_unit(u: tuple[str, ...]) -> bool:
    return u[0] in ORDERED_PREFIXES or (
        len(u) == 1 and u[0].startswith(ORDERED_PREFIXES) and not u[0].startswith("-isysroot")
    )


def _canon_unit(u: tuple[str, ...]) -> tuple[str, ...]:
    # -Ifoo and -I foo are the same unit.
    if len(u) == 1:
        for p in ("-isystem", "-iquote", "-idirafter", "-I", "-F", "-D", "-U", "-L", "-l"):
            if u[0].startswith(p) and len(u[0]) > len(p):
                return (p, u[0][len(p) :])
    return u


def diff_set(a: list[str], b: list[str]) -> dict | None:
    ua = [_canon_unit(u) for u in units(a)]
    ub = [_canon_unit(u) for u in units(b)]
    # The tool (argv[0]) is always compared positionally.
    res: dict = {}
    if ua[:1] != ub[:1]:
        res["tool"] = [" ".join(ua[0]) if ua else None, " ".join(ub[0]) if ub else None]
    ca, cb = Counter(ua[1:]), Counter(ub[1:])
    only_a = sorted(" ".join(u) for u in (ca - cb).elements())
    only_b = sorted(" ".join(u) for u in (cb - ca).elements())
    if only_a:
        res["only_a"] = only_a
    if only_b:
        res["only_b"] = only_b
    # Include order is compared over the search paths both sides have, so it is
    # reported even when the sets also differ.
    oa = [u for u in ua if _is_ordered_unit(u)]
    ob = [u for u in ub if _is_ordered_unit(u)]
    oa, ob = [u for u in oa if u in ob], [u for u in ob if u in oa]
    if oa != ob:
        res["include_order"] = [[" ".join(u) for u in oa], [" ".join(u) for u in ob]]
    return res or None


def diff_ordered(a: list[str], b: list[str]) -> dict | None:
    if a == b:
        return None
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    ops = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        ops.append({"op": tag, "at": i1, "a": a[i1:i2], "b": b[j1:j2]})
    return {"ops": ops}


def diff_argv(kind: str, a: list[str] | None, b: list[str] | None, modes: dict) -> dict | None:
    if a is None and b is None:
        return None
    if a is None or b is None:
        return {"missing_argv": "a" if a is None else "b"}
    mode = modes.get(kind, "ordered")
    return diff_set(a, b) if mode == "set" else diff_ordered(a, b)


# --------------------------------------------------------------------------- matching


@dataclass
class Result:
    rows: dict = field(default_factory=lambda: defaultdict(Counter))
    diffs: list = field(default_factory=list)
    only_a: list = field(default_factory=list)
    only_b: list = field(default_factory=list)

    def totals(self) -> Counter:
        t: Counter = Counter()
        for c in self.rows.values():
            t.update(c)
        return t


def _drop_flags(argv: list[str], flags: set[str]) -> list[str]:
    out, skip = [], False
    for t in argv:
        if skip:
            skip = False
        elif t in flags:
            skip = True
        else:
            out.append(t)
    return out


def _prep(records: list[dict], side: str, pm: PathMap, include_excluded: bool) -> list[dict]:
    out = []
    for r in records:
        if r.get("excluded") and not include_excluded:
            continue
        r = map_record(r, side, pm)
        if pm.ignore or pm.ignore_flags:
            r = copy.deepcopy(r)
            keep = lambda t: not any(rx.fullmatch(t) for rx in pm.ignore)
            if "argv" in r:
                r["argv"] = [t for t in _drop_flags(r["argv"], pm.ignore_flags) if keep(t)]
            for st in r.get("steps", []):
                st["argv"] = [t for t in _drop_flags(st["argv"], pm.ignore_flags) if keep(t)]
        out.append(r)
    return out


def compare(
    a_recs: list[dict],
    b_recs: list[dict],
    pm: PathMap | None = None,
    modes: dict | None = None,
    full_argv: bool = False,
    include_excluded: bool = False,
    check_inputs: bool = False,
) -> Result:
    pm = pm or PathMap()
    modes = {**DEFAULT_MODES, **pm.modes, **(modes or {})}
    A = _prep(a_recs, "a", pm, include_excluded)
    B = _prep(b_recs, "b", pm, include_excluded)
    b_by_id = {r["id"]: r for r in B}
    b_by_out: dict[str, dict] = {}
    for r in B:
        for o in r.get("outputs", []):
            b_by_out.setdefault(o, r)
    used: set[str] = set()
    res = Result()
    for ra in A:
        rb = b_by_id.get(ra["id"])
        if rb is None or rb["id"] in used:
            rb = next(
                (
                    b_by_out[o]
                    for o in ra.get("outputs", [])
                    if o in b_by_out and b_by_out[o]["id"] not in used
                ),
                None,
            )
        key = (ra.get("phase"), ra.get("kind"))
        if rb is None:
            res.rows[key]["only_a"] += 1
            res.only_a.append(ra["id"])
            continue
        used.add(rb["id"])
        d: dict = {}
        if ra.get("kind") != rb.get("kind"):
            d["kind_mismatch"] = [ra.get("kind"), rb.get("kind")]
        ad = diff_argv(
            ra.get("kind", ""), record_argv(ra, full_argv), record_argv(rb, full_argv), modes
        )
        if ad:
            d["argv"] = ad
        if check_inputs:
            for k in ("explicit", "implicit", "order_only"):
                ia = set(ra.get("inputs", {}).get(k, []))
                ib = set(rb.get("inputs", {}).get(k, []))
                if ia != ib:
                    d.setdefault("inputs", {})[k] = {
                        "only_a": sorted(ia - ib),
                        "only_b": sorted(ib - ia),
                    }
        if d:
            res.rows[key]["differ"] += 1
            res.diffs.append(
                {"id": ra["id"], "b_id": rb["id"], "phase": key[0], "kind": key[1], **d}
            )
        else:
            res.rows[key]["match"] += 1
    for rb in B:
        if rb["id"] not in used:
            res.rows[(rb.get("phase"), rb.get("kind"))]["only_b"] += 1
            res.only_b.append(rb["id"])
    return res


# --------------------------------------------------------------------------- report


def _fmt_diff(d: dict) -> list[str]:
    lines = [f"  {d['id']}" + (f"  (B: {d['b_id']})" if d["b_id"] != d["id"] else "")]
    if "kind_mismatch" in d:
        lines.append(f"    kind: {d['kind_mismatch'][0]} != {d['kind_mismatch'][1]}")
    ad = d.get("argv", {})
    if "tool" in ad:
        lines.append(f"    tool: {ad['tool'][0]} != {ad['tool'][1]}")
    for u in ad.get("only_a", []):
        lines.append(f"    - {u}")
    for u in ad.get("only_b", []):
        lines.append(f"    + {u}")
    if "include_order" in ad:
        lines.append(f"    include order A: {' '.join(ad['include_order'][0])}")
        lines.append(f"    include order B: {' '.join(ad['include_order'][1])}")
    for op in ad.get("ops", []):
        lines.append(f"    @{op['at']} {op['op']}: -{op['a']} +{op['b']}")
    if "missing_argv" in ad:
        lines.append(f"    argv missing on side {ad['missing_argv']}")
    for k, v in d.get("inputs", {}).items():
        lines.append(f"    inputs.{k}: -{v['only_a']} +{v['only_b']}")
    return lines


def report(res: Result, details: int = 10) -> str:
    lines = [f"{'phase':>5}  {'kind':<9} {'match':>6} {'differ':>6} {'only_a':>6} {'only_b':>6}"]
    for (ph, kind), c in sorted(res.rows.items(), key=lambda kv: (kv[0][0] or 99, kv[0][1] or "")):
        lines.append(
            f"{ph if ph is not None else '-':>5}  {kind or '-':<9} "
            f"{c['match']:>6} {c['differ']:>6} {c['only_a']:>6} {c['only_b']:>6}"
        )
    t = res.totals()
    lines.append(
        f"{'total':>5}  {'':<9} {t['match']:>6} {t['differ']:>6} {t['only_a']:>6} {t['only_b']:>6}"
    )
    if details:
        if res.diffs:
            lines.append(f"\nargv-differing (first {details} of {len(res.diffs)}):")
            for d in res.diffs[:details]:
                lines += _fmt_diff(d)
        for name, ids in (("only in A", res.only_a), ("only in B", res.only_b)):
            if ids:
                lines.append(f"\n{name} (first {details} of {len(ids)}):")
                lines += [f"  {i}" for i in ids[:details]]
    return "\n".join(lines)


def to_json(res: Result) -> dict:
    return {
        "rows": [
            {"phase": ph, "kind": k, **dict(c)}
            for (ph, k), c in sorted(
                res.rows.items(), key=lambda kv: (kv[0][0] or 99, kv[0][1] or "")
            )
        ],
        "totals": dict(res.totals()),
        "diffs": res.diffs,
        "only_a": res.only_a,
        "only_b": res.only_b,
    }


# --------------------------------------------------------------------------- self-test


def _first(recs: list[dict], pred) -> dict:
    return next(r for r in recs if not r.get("excluded") and pred(r))


def _main_argv(r: dict) -> list[str]:
    return r["steps"][r["main"]]["argv"]


def perturb(recs: list[dict]) -> tuple[list[dict], dict]:
    """A perturbed copy of ``recs`` and the ids of each perturbation."""
    b = copy.deepcopy(recs)
    compiles = [
        r
        for r in b
        if not r.get("excluded") and r.get("kind") == "compile" and r.get("main") is not None
    ]
    links = [
        r
        for r in b
        if not r.get("excluded")
        and r.get("kind") == "link"
        and sum(t.endswith((".o", ".a", ".dylib")) for t in _main_argv(r)) >= 2
    ]
    dropped, changed, reordered = compiles[0], compiles[1], compiles[2]
    link = links[0]
    b.remove(dropped)
    av = _main_argv(changed)
    # Change one flag: replace the first -D or -O token, else append one.
    idx = next((i for i, t in enumerate(av) if t.startswith(("-O", "-D"))), None)
    if idx is None:
        av.append("-DBUILDLOG_PERTURBED")
    else:
        av[idx] = av[idx] + "_PERTURBED"
    av = _main_argv(reordered)
    # Reorder non-include flags only (swap the first two -D/-W units); must still match.
    pos = [i for i, t in enumerate(av) if t.startswith(("-W", "-D", "-f"))]
    if len(pos) >= 2:
        i, j = pos[0], pos[-1]
        av[i], av[j] = av[j], av[i]
    av = _main_argv(link)
    objs = [i for i, t in enumerate(av) if t.endswith((".o", ".a", ".dylib"))]
    i, j = objs[0], objs[-1]
    av[i], av[j] = av[j], av[i]
    return b, {
        "dropped": dropped["id"],
        "changed": changed["id"],
        "reordered_compile": reordered["id"],
        "reordered_link": link["id"],
    }


def self_test(recs: list[dict], out=sys.stdout) -> bool:
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        ok &= cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}", file=out)

    print("self-test: baseline vs itself", file=out)
    r = compare(recs, recs)
    t = r.totals()
    n = sum(1 for x in recs if not x.get("excluded"))
    check(f"all {n} in-scope records match", t["match"] == n and not r.diffs)
    check("nothing only on one side", not r.only_a and not r.only_b)

    print("self-test: baseline vs perturbed copy", file=out)
    b, ids = perturb(recs)
    r = compare(recs, b)
    diff_ids = {d["id"] for d in r.diffs}
    check("dropped compile is only in A", r.only_a == [ids["dropped"]])
    check("changed compile flag differs", ids["changed"] in diff_ids)
    orig = {x["id"]: x for x in recs}
    moved_b = {x["id"]: x for x in b}
    rc = ids["reordered_compile"]
    check(
        "reordered compile flags still match (argv really reordered)",
        rc not in diff_ids and _main_argv(orig[rc]) != _main_argv(moved_b[rc]),
    )
    check("reordered link inputs differ", ids["reordered_link"] in diff_ids)
    check("exactly two argv differences", len(r.diffs) == 2)
    check("nothing only in B", not r.only_b)

    print("self-test: relocated output layout + path map", file=out)
    moved = [map_record(x, "b", _relocate_map()) for x in recs]
    r = compare(recs, moved)
    check("relocated layout without a map does not match", r.totals()["match"] < n / 2)
    back = PathMap(rules=[("b", re.compile(r"\$BUILD/out/"), "$BUILD/")])
    r = compare(recs, moved, pm=back)
    check("path map restores all matches", r.totals()["match"] == n and not r.diffs)
    print("self-test:", "PASS" if ok else "FAIL", file=out)
    return ok


def _relocate_map() -> PathMap:
    return PathMap(rules=[("b", re.compile(r"\$BUILD/"), "$BUILD/out/")])


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("a", nargs="?")
    ap.add_argument("b", nargs="?")
    ap.add_argument("--map", help="TOML path map / mode overrides")
    ap.add_argument("--mode", action="append", default=[], metavar="KIND=set|ordered")
    ap.add_argument("--full-argv", action="store_true", help="compare the whole command line")
    ap.add_argument("--include-excluded", action="store_true")
    ap.add_argument("--check-inputs", action="store_true", help="also compare input sets")
    ap.add_argument("--phase", type=int, action="append", help="restrict to phase(s)")
    ap.add_argument("--details", type=int, default=10, help="diff entries to print (0: none)")
    ap.add_argument("--json", help="write the full result as JSON")
    ap.add_argument("--fail-on-diff", action="store_true", help="exit 1 unless identical")
    ap.add_argument("--self-test", metavar="BASE", help="run the self-test on a baseline")
    args = ap.parse_args(argv)

    if args.self_test:
        return 0 if self_test(load_jsonl(args.self_test)) else 1
    if not (args.a and args.b):
        ap.error("need A.jsonl and B.jsonl (or --self-test BASE)")
    modes = {}
    for m in args.mode:
        k, _, v = m.partition("=")
        if v not in ("set", "ordered"):
            ap.error(f"--mode {m}: mode must be set or ordered")
        modes[k] = v
    a, b = load_jsonl(args.a), load_jsonl(args.b)
    if args.phase:
        a = [r for r in a if r.get("phase") in args.phase]
        b = [r for r in b if r.get("phase") in args.phase]
    res = compare(
        a,
        b,
        pm=PathMap.load(args.map),
        modes=modes,
        full_argv=args.full_argv,
        include_excluded=args.include_excluded,
        check_inputs=args.check_inputs,
    )
    print(report(res, args.details))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(to_json(res), f, indent=1, sort_keys=True)
            f.write("\n")
    t = res.totals()
    return 1 if args.fail_on_diff and (t["differ"] or t["only_a"] or t["only_b"]) else 0


if __name__ == "__main__":
    sys.exit(main())
