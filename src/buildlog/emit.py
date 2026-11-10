# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Translation layer: L0 facts -> build.ninja.

"Linearize and serialize, never derive." This file knows about Ninja syntax,
path roots, and how to put a partial order into a sequence. It does not
know about languages, toolchains, targets, or configurations. Every
token it emits comes from an L0 fact.

  usage: emit.py FACT_DIR PROJECT_DIR BUILD_DIR
"""

import csv
import heapq
import json
import os
import re
import shlex
import sys
from collections import defaultdict
from functools import cache
from pathlib import Path

# The L0 relations this translator reads; the loader outputs exactly these.
RELATIONS = (
    "action",
    "action_failed",
    "action_generator",
    "action_out",
    "action_in",
    "action_depfile",
    "action_arg",
    "action_arg_join",
    "action_arg_part",
    "action_seq",
    "action_seq_before",
    "action_seq_spelled",
    "action_seq_spelled_suffix",
    "action_seq_same_group",
    "action_seq_group_delims",
    "action_stdin",
    "action_stdout",
    "action_cwd",
    "action_restat",
    "action_dyndep",
    "action_builtin",
    "builtin_argv",
    "default_output",
    "test_case",
    "test_case_arg",
    "test_case_cwd",
    "test_case_env",
    "test_case_env_path",
    "test_case_label",
    "test_case_regex",
    "test_case_serial",
    "test_case_needs",
    "test_case_skip",
    "test_case_launcher",
)


def load(fact_dir, rel):
    if rel not in RELATIONS:
        raise ValueError(f"emit reads {rel}, which is missing from emit.RELATIONS")
    path = os.path.join(fact_dir, rel + ".csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return [tuple(r) for r in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)]


# What shlex.quote leaves unquoted, plus spaces (a prefix may be several
# words, "-MD -MF "): a prefix with anything else ($ORIGIN) is quoted.
_SHELL_SPECIAL = re.compile(r"[^\w@%+=:,./ -]")


def _workspace_root(project_dir):
    """The outermost ancestor of the project holding a .git entry (the
    checkout's root: a submodule's own .git sits below it), else the project."""
    d = os.path.abspath(project_dir)
    found = [str(c) for c in (Path(d), *Path(d).parents) if (c / ".git").exists()]
    return found[-1] if found else d


def main(fact_dir, project_dir, build_dir):
    src_rel = os.path.relpath(project_dir, build_dir)
    root = os.path.abspath(build_dir)
    ws = _workspace_root(project_dir)

    quote = cache(shlex.quote)

    @cache
    def lex(item):
        """The lexical tie-break of paths, independent of where the checkout
        is: the workspace's root is replaced by a name that sorts after every
        other absolute path (system and package-manager prefixes), so the order
        is that of a checkout under such a root wherever it really is."""
        return item.replace(ws, "/\U0010ffff") if ws != os.sep else item

    @cache
    def rel(v):  # root relativization (backend-relative)
        if v.startswith("src:"):
            return os.path.join(src_rel, v[4:])
        if v.startswith("out:"):
            return v[4:]
        return v

    def esc_path(p):  # ninja path escaping
        return p.replace("$", "$$").replace(" ", "$ ").replace(":", "$:")

    def esc_cmd(s):
        return s.replace("$", "$$")

    @cache
    def qpre(p):  # an argument's prefix, quoted only if the shell would read it
        if not _SHELL_SPECIAL.search(p):
            return p
        body = p.rstrip(" ")
        return quote(body) + p[len(body) :]

    actions = dict(load(fact_dir, "action"))
    outs, ins, depfile = defaultdict(set), defaultdict(lambda: defaultdict(set)), {}
    argv, seq, before, same = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
    delims, failed = {}, defaultdict(set)
    # Standard streams, typed in L0 (at most one each; L0 checks it).
    stdin, stdout = dict(load(fact_dir, "action_stdin")), dict(load(fact_dir, "action_stdout"))
    # Working directory, restat, builtins (at most one cwd/builtin; L0 checks it).
    cwd, builtin = dict(load(fact_dir, "action_cwd")), dict(load(fact_dir, "action_builtin"))
    restat = {a for (a,) in load(fact_dir, "action_restat")}
    dyndep = dict(load(fact_dir, "action_dyndep"))
    builtin_argv = [v for _, v in sorted((int(i), v) for i, v in load(fact_dir, "builtin_argv"))]
    for a, kind, detail in load(fact_dir, "action_failed"):
        failed[a].add(f"error: {kind}: {detail}")
    for a, p in load(fact_dir, "action_out"):
        outs[a].add(p)
    for a, p, kind in load(fact_dir, "action_in"):
        ins[a][kind].add(p)
    for a, p in load(fact_dir, "action_depfile"):
        if a in depfile:
            sys.exit(f"error: multiple depfiles for {a}")
        depfile[a] = p
    for a, rank, prefix, value in load(fact_dir, "action_arg"):
        argv[a].add((int(rank), prefix, value))
    # A word of several paths: prefix ++ its parts, in order, joined by sep.
    join, parts = {}, defaultdict(list)
    for a, rank, prefix, sep in load(fact_dir, "action_arg_join"):
        join[(a, int(rank))] = (prefix, sep)
    for a, rank, i, value in load(fact_dir, "action_arg_part"):
        parts[(a, int(rank))].append((int(i), value))
    for a, rank, item in load(fact_dir, "action_seq"):
        seq[(a, int(rank))].add(item)
    for a, rank, x, y in load(fact_dir, "action_seq_before"):
        before[(a, int(rank))].add((x, y))
    spelled = {(a, int(r), i): (p, v) for a, r, i, p, v in load(fact_dir, "action_seq_spelled")}
    suffix = {(a, int(r), i): s for a, r, i, s in load(fact_dir, "action_seq_spelled_suffix")}

    for a, rank, x, y in load(fact_dir, "action_seq_same_group"):
        same[(a, int(rank))].add((x, y))
    for a, rank, o, c in load(fact_dir, "action_seq_group_delims"):
        if (a, int(rank)) in delims:
            sys.exit(f"error: multiple group delimiters for {a} segment {rank}")
        delims[(a, int(rank))] = (o, c)

    def toposort(key):
        """Order a segment: collapse declared groups (union-find), toposort the
        quotient with the lexically smallest member as tie-break key, expand
        each group's members lexically, and wrap multi-member groups in the
        segment's delimiters if any. Undeclared cycles are errors."""
        items = seq[key]
        parent = {i: i for i in items}

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for x, y in same[key]:
            if x in parent and y in parent:
                rx, ry = find(x), find(y)
                if rx != ry:
                    parent[max(rx, ry, key=lex)] = min(rx, ry, key=lex)
        members = defaultdict(list)
        for i in items:
            members[find(i)].append(i)
        group_key = {g: min(map(lex, ms)) for g, ms in members.items()}
        succ, indeg = defaultdict(set), {g: 0 for g in members}
        for x, y in before[key]:
            gx, gy = find(x), find(y)
            if gx != gy and gy not in succ[gx]:
                succ[gx].add(gy)
                indeg[gy] += 1
        heap = [(group_key[g], g) for g, d in indeg.items() if d == 0]
        heapq.heapify(heap)
        order = []
        while heap:
            _, g = heapq.heappop(heap)
            ms = sorted(members[g], key=lex)
            if len(ms) > 1 and key in delims:
                order += [delims[key][0], *ms, delims[key][1]]
            else:
                order += ms
            for h in succ[g]:
                indeg[h] -= 1
                if indeg[h] == 0:
                    heapq.heappush(heap, (group_key[h], h))
        if sum(len(m) for m in members.values()) != len(items) or len(
            [g for g in members if indeg[g] > 0]
        ):
            sys.exit(f"error: undeclared cycle in ordered segment {key}")
        return order

    lines = [
        "# Generated by emit.py from L0 facts. Do not edit.",
        f"ninja_required_version = {'1.11' if dyndep else '1.10'}",
        "",
        "rule run",
        "  command = $cmd",
        "  description = $label $out",
        "",
        "rule run_dep",
        "  command = $cmd",
        "  description = $label $out",
        "  depfile = $dep",
        "  deps = gcc",
        "",
    ]
    generators = {a for (a,) in load(fact_dir, "action_generator")}
    if generators:
        lines += [
            "rule regen",
            "  command = $cmd",
            "  description = $label $out",
            "  generator = 1",
            "",
        ]
    if failed:
        # L0 marks actions that errors break (action_failed); each becomes an
        # edge without inputs that prints the errors and fails.
        lines += ["rule fail", "  command = $cmd", "  description = FAILED $label $out", ""]
    # Each action's ordered segments and joined words, in seq's and join's order.
    seq_ranks, join_ranks = defaultdict(list), defaultdict(list)
    for aa, r in seq:
        seq_ranks[aa].append(r)
    for aa, r in join:
        join_ranks[aa].append(r)
    created_cwds = set()
    for a in sorted(actions):
        o = " ".join(esc_path(rel(x)) for x in [a])
        extra = sorted(outs[a])
        if extra:
            o += " | " + " ".join(esc_path(rel(x)) for x in extra)
        if a in failed:
            msg = " ".join(quote(m) for m in sorted(failed[a]))
            lines.append(f"build {o}: fail")
            lines.append(f"  label = {actions[a]}")
            command = "printf '%s\\n' " + msg + " >&2; exit 1"
            lines.append(f"  cmd = {esc_cmd(command)}")
            lines.append("")
            continue
        tokens = [(r, p, v) for (r, p, v) in argv[a]]
        tokens += [(r, None, None) for r in seq_ranks.get(a, ())]
        tokens += [(r, "", None) for r in join_ranks.get(a, ())]
        here = os.path.normpath(rel(cwd[a])) if a in cwd else None

        def arg(v, first=False, here=here):
            """A token for the command: rooted paths are rebased onto its cwd,
            and a bare program name there becomes ./name (not a PATH search)."""
            r = rel(v)
            if here is None or r == v:
                return r
            r = os.path.relpath(os.path.join(root, r), os.path.join(root, here))
            return f"./{r}" if first and "/" not in r else r

        cmd = [quote(t) for t in [*builtin_argv, builtin[a]]] if a in builtin else []
        for r, p, v in sorted(tokens, key=lambda t: (t[0], t[1] or "", lex(t[2] or ""))):
            if v is None and p is not None:
                pre, sep = join[(a, r)]
                word = sep.join(arg(x) for _, x in sorted(parts[(a, r)]))
                cmd.append(qpre(pre) + quote(word))
            elif p is None:
                for i in toposort((a, r)):
                    sp = spelled.get((a, r, i))
                    if sp:
                        word = qpre(sp[0]) + quote(arg(sp[1]))
                        cmd.append(word + qpre(suffix.get((a, r, i), "")))
                    else:
                        cmd.append(arg(i))
            else:
                cmd.append(qpre(p) + quote(arg(v, first=r == 0 and not p)))
        # Ninja runs commands with /bin/sh: the streams are its redirections,
        # and the working directory is a `cd` before the command.
        if a in stdin:
            cmd += ["<", quote(arg(stdin[a]))]
        if a in stdout:
            cmd += [">", quote(arg(stdout[a]))]
        if here is not None:
            cmd = ["cd", quote(here), "&&", *cmd]
            if cwd[a].startswith("out:") and here not in created_cwds:
                os.makedirs(os.path.join(build_dir, here), exist_ok=True)
                created_cwds.add(here)
        i = " ".join(esc_path(rel(x)) for x in sorted(ins[a]["explicit"], key=lex))
        if ins[a]["implicit"]:
            i += " | " + " ".join(esc_path(rel(x)) for x in sorted(ins[a]["implicit"], key=lex))
        if ins[a]["order_only"]:
            i += " || " + " ".join(esc_path(rel(x)) for x in sorted(ins[a]["order_only"], key=lex))
        rule = "regen" if a in generators else "run_dep" if a in depfile else "run"
        lines.append(f"build {o}: {rule} {i}".rstrip())
        lines.append(f"  label = {actions[a]}")
        lines.append(f"  cmd = {esc_cmd(' '.join(cmd))}")
        if a in depfile:
            lines.append(f"  dep = {esc_cmd(rel(depfile[a]))}")
        if a in restat:
            lines.append("  restat = 1")
        if a in dyndep:
            lines.append(f"  dyndep = {esc_path(rel(dyndep[a]))}")
        lines.append("")
    defaults = sorted(rel(p) for (p,) in load(fact_dir, "default_output"))
    if defaults:
        lines.append("default " + " ".join(esc_path(d) for d in defaults))
    os.makedirs(build_dir, exist_ok=True)
    with open(os.path.join(build_dir, "build.ninja"), "w") as f:
        f.write("\n".join(lines) + "\n")
    write_tests(fact_dir, build_dir, rel, root)


def write_tests(fact_dir, build_dir, rel, root):
    """tests.json: one record per L0 test case, sorted; argv paths rebased
    onto the case's working directory (a bare program name there becomes
    ./name), needs and cwd relative to the build directory. No cases: no
    file (a stale one is removed)."""
    path = os.path.join(build_dir, "tests.json")
    cases = sorted(load(fact_dir, "test_case"))
    if not cases:
        if os.path.exists(path):
            os.remove(path)
        return
    by = defaultdict(lambda: defaultdict(list))
    for rel_name, n in (
        ("test_case_arg", 2),
        ("test_case_cwd", 1),
        ("test_case_env", 2),
        ("test_case_env_path", 3),
        ("test_case_label", 1),
        ("test_case_regex", 2),
        ("test_case_needs", 1),
        ("test_case_skip", 1),
        ("test_case_launcher", 1),
    ):
        for row in load(fact_dir, rel_name):
            by[rel_name][tuple(row[:2])].append(tuple(row[2 : 2 + n]))
    serial = {tuple(r) for r in load(fact_dir, "test_case_serial")}

    def env_paths(rows):
        d = defaultdict(list)
        for k, i, p in sorted(rows, key=lambda r: (r[0], int(r[1]))):
            d[k].append(rel(p))
        return dict(d)

    out = []
    for key in cases:
        (cwd,) = by["test_case_cwd"][key][0] if by["test_case_cwd"][key] else ("out:",)
        here = os.path.normpath(rel(cwd))

        def arg(v, first=False, here=here):
            r = rel(v)
            if r == v:
                return r
            r = os.path.relpath(os.path.join(root, r), os.path.join(root, here))
            return f"./{r}" if first and "/" not in r else r

        argv = [arg(v, i == 0) for i, v in sorted((int(i), v) for i, v in by["test_case_arg"][key])]
        regex = defaultdict(list)
        for kind, r in sorted(by["test_case_regex"][key]):
            regex[kind].append(r)
        out.append({
            "name": key[0],
            "config": key[1],
            "argv": argv,
            "cwd": here,
            "env": dict(sorted(by["test_case_env"][key])),
            "env_path": env_paths(by["test_case_env_path"][key]),
            "labels": sorted(label for (label,) in by["test_case_label"][key]),
            "pass": regex["pass"],
            "fail": regex["fail"],
            "skip": regex["skip"],
            "serial": key in serial,
            "needs": sorted(rel(p) for (p,) in by["test_case_needs"][key] if not rel(p).startswith("/")),
            "not_run": sorted(r for (r,) in by["test_case_skip"][key]),
            # platform_runs_on's command prefix (an emulator), before argv
            "launcher": [w for (v,) in sorted(by["test_case_launcher"][key]) for w in shlex.split(v)],
        })  # fmt: skip
    with open(path, "w") as f:
        json.dump({"format": "buildlog-tests", "version": 1, "tests": out}, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main(*sys.argv[1:4])
