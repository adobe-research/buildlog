# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compare a normalized Bazel action graph (normalize_aquery.py) with a normalized
buildlog graph (case-studies/halide/baseline/normalize.py on buildlog's build.ninja).

  compare.py BAZEL.jsonl BUILDLOG.jsonl --role CFG=ROLE[,ROLE]... [--details N]

Bazel records carry their configuration role (target or exec). buildlog's do not:
its configuration is a directory component (bin/<cfg>/, obj/<cfg>/...), and
``--role`` says which role each buildlog configuration plays in this build, e.g.
``--role arm64-macos.debug.none=target --role arm64-macos.release.none=exec``. A
configuration that is both (a native release build, where the exec configuration
is the target configuration) is ``--role arm64-macos.release.none=target,exec``;
each of its actions counts for the roles Bazel builds it in (one buildlog
instance against Bazel's target and exec copies).

Actions are matched per (role, kind, unit): a compile by its source file, an
archive, link or custom command by its output's file name (Bazel's per-target
archives have no buildlog counterpart and are reported as such). Matched actions
are compared by their flags: the main step's tokens without paths, outputs and
dependency-file bookkeeping, as a multiset. Include and framework search paths are
counted, not compared (the layouts differ by design: _virtual_includes vs source
dirs). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

# Tokens that take a path argument, dropped with it.
PATH_FLAGS = {"-o", "-MF", "-MT", "-MQ", "-iquote", "-isystem", "-I", "-isysroot", "-F"}
PATH_PREFIXED = ("-I", "-F", "-frandom-seed=", "-fdebug-prefix-map=", "-Wl,-oso_prefix,")
INCLUDE_FLAGS = {"-I", "-iquote", "-isystem"}


def load(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def is_path(tok: str) -> bool:
    return tok.startswith("$") or "/" in tok or tok.endswith((".o", ".a", ".cpp", ".cc", ".fbs"))


def flags(argv: list[str]) -> tuple[Counter, int]:
    """(flag multiset, number of include/framework search paths) of a main step."""
    out: Counter = Counter()
    includes = 0
    skip = False
    for i, tok in enumerate(argv[1:], 1):
        if skip:
            skip = False
            continue
        if tok in PATH_FLAGS:
            includes += tok in INCLUDE_FLAGS
            skip = True
            continue
        if tok == "-c" and i + 1 < len(argv):
            skip = True
            continue
        if tok.startswith(PATH_PREFIXED) and len(tok) > 2:
            includes += tok.startswith("-I")
            continue
        if tok in ("-MD", "-MMD") or is_path(tok):
            continue
        out[tok] += 1
    return out, includes


def main_argv(r: dict) -> list[str]:
    return r["steps"][r.get("main", 0)]["argv"]


def unit(r: dict) -> str | None:
    argv = main_argv(r)
    if r["kind"] == "compile":
        return argv[argv.index("-c") + 1] if "-c" in argv else None
    return os.path.basename(r["id"])


_CFG = re.compile(r"\$BUILD/(?:bin|lib|obj|gen|include)/([^/]+)/")


def buildlog_roles(r: dict, roles: dict[str, list[str]]) -> list[str]:
    m = _CFG.search(r["id"])
    return roles.get(m.group(1), []) if m else []


def index(records: list[dict], role_of) -> tuple[dict[tuple, dict], Counter]:
    """(role, kind, unit) -> the first record, and how many records share each key."""
    out: dict[tuple, dict] = {}
    count: Counter = Counter()
    for r in records:
        if "excluded" in r or r["kind"] not in ("compile", "link", "archive", "custom"):
            continue
        u = unit(r)
        if u is None:
            continue
        for role in role_of(r):
            out.setdefault((role, r["kind"], u), r)
            count[(role, r["kind"], u)] += 1
    return out, count


def compare(
    bazel: list[dict], buildlog: list[dict], roles: dict[str, list[str]], details: int
) -> str:
    b, bn = index(bazel, lambda r: [r["config_role"]])

    def role_of(r: dict) -> list[str]:
        # A configuration with several roles (exec = target) holds one instance
        # per target; it counts for each role Bazel builds that action in.
        rs = buildlog_roles(r, roles)
        if len(rs) > 1:
            u = unit(r)
            hit = [x for x in rs if (x, r["kind"], u) in b]
            return hit or rs[:1]
        return rs

    s, sn = index(buildlog, role_of)
    shared = {c for c, rs in roles.items() if len(rs) > 1}
    lines = []
    lines.append(
        "| role | kind | actions Bazel | actions buildlog | both | only Bazel | only buildlog |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    keys = sorted({k[:2] for k in b} | {k[:2] for k in s})
    for role, kind in keys:
        bk = {k for k in b if k[:2] == (role, kind)}
        sk = {k for k in s if k[:2] == (role, kind)}
        na = sum(bn[k] for k in bk)
        ns = sum(sn[k] for k in sk)
        lines.append(
            f"| {role} | {kind} | {na} | {ns} | {len(bk & sk)} | {len(bk - sk)} | {len(sk - bk)} |"
        )
    lines.append("")
    only_b = sorted(set(b) - set(s))
    only_s = sorted(set(s) - set(b))
    dups = sorted(k for k, n in bn.items() if n > 1)
    if dups:
        lines.append(
            "Bazel compiles one source twice in one configuration (two targets list it): "
            + ", ".join(f"{r}/{u}" for r, _, u in dups)
        )
    if only_b:
        lines.append("only Bazel: " + ", ".join(f"{r}/{k}/{u}" for r, k, u in only_b))
    if only_s:
        lines.append("only buildlog: " + ", ".join(f"{r}/{k}/{u}" for r, k, u in only_s))
    if shared:
        per = Counter(id(r) for r in s.values())
        n = sum(1 for c in per.values() if c > 1)
        lines.append(f"buildlog actions serving both roles (one instance where Bazel has two): {n}")
    lines.append("")
    # Flag differences, aggregated per (role, kind): which flags only one side has,
    # and in how many of the matched actions.
    agg: dict[tuple, dict[str, Counter]] = defaultdict(
        lambda: {"bazel": Counter(), "buildlog": Counter()}
    )
    inc: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    matched = Counter()
    for k in sorted(set(b) & set(s)):
        role, kind, _ = k
        fb, ib = flags(main_argv(b[k]))
        fs, is_ = flags(main_argv(s[k]))
        matched[(role, kind)] += 1
        for t in set(fb) | set(fs):
            if fb[t] and not fs[t]:
                agg[(role, kind)]["bazel"][t] += 1
            elif fs[t] and not fb[t]:
                agg[(role, kind)]["buildlog"][t] += 1
        inc[(role, kind)].append((ib, is_))
    for rk in sorted(matched):
        lines.append(f"{rk[0]} {rk[1]} ({matched[rk]} matched):")
        for side in ("bazel", "buildlog"):
            c = agg[rk][side]
            if c:
                items = sorted(c.items(), key=lambda x: (-x[1], x[0]))[:details]
                lines.append(f"  only {side}: " + " ".join(f"{t}[{n}]" for t, n in items))
        if rk[1] == "compile":
            ib = sorted({x for x, _ in inc[rk]})
            is_ = sorted({y for _, y in inc[rk]})
            lines.append(f"  search paths: Bazel {ib}, buildlog {is_}")
        if rk[1] in ("custom",):
            for k in sorted(set(b) & set(s)):
                if k[:2] == rk:
                    lines.append(f"  Bazel:    {' '.join(main_argv(b[k]))}")
                    lines.append(f"  buildlog: {' '.join(main_argv(s[k]))}")
                    tb, ts = b[k].get("tool_config"), buildlog_roles_tool(s[k], roles)
                    lines.append(f"  tool built in: Bazel {tb}, buildlog {ts}")
    return "\n".join(lines) + "\n"


def buildlog_roles_tool(r: dict, roles: dict[str, list[str]]) -> str:
    m = _CFG.search(r.get("tool", ""))
    return "+".join(roles.get(m.group(1), ["?"])) + f" ({m.group(1)})" if m else "not built"


def parse_roles(items: list[str]) -> dict[str, list[str]]:
    out = {}
    for item in items:
        cfg, sep, rs = item.partition("=")
        if not sep:
            sys.exit(f"--role: expected CFG=ROLE[,ROLE], got {item!r}")
        out[cfg] = rs.split(",")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("bazel")
    ap.add_argument("buildlog")
    ap.add_argument("--role", action="append", default=[], metavar="CFG=ROLE[,ROLE]")
    ap.add_argument("--details", type=int, default=30)
    args = ap.parse_args(argv)
    print(
        compare(load(args.bazel), load(args.buildlog), parse_roles(args.role), args.details), end=""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
