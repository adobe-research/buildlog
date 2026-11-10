# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Normalize a Bazel action graph (``bazel aquery --output=jsonproto``) into the
case studies' canonical JSONL records.

The CMake baselines are normalized from Ninja files by
case-studies/halide/baseline/normalize.py; this tool writes the same record shape
from Bazel's analysis-time action graph, so a Bazel build, a CMake build and a
buildlog build can be compared with one set of scripts. Bazel has two things a
Ninja file does not: every action runs in a *configuration* (target or exec, with
an output directory named by its mnemonic), and every action belongs to a
configured *target* (a label and its rule class). Both are kept.

  normalize_aquery.py normalize AQUERY.json [--variant NAME] [--map PREFIX=PLACEHOLDER]...
                      [--main PLACEHOLDER] [--exclude-label REGEX=REASON]... -o OUT.jsonl
  normalize_aquery.py summary OUT.jsonl

Records (one per action, sorted by ``id``, keys sorted) have the fields of
normalize.py's records (id, outputs, inputs, kind, rule, steps, main, argv, tool,
tool_name, tool_built, runs_built, target, variant, excluded), plus:

- ``configuration``: the configuration's mnemonic, e.g. ``darwin_arm64-opt-exec-ST-d57f47055a04``;
- ``config_role``: ``target`` or ``exec`` (Bazel's ``isTool``); several configurations of
  one role are told apart by platform, then by id (``target:darwin_x86_64``);
- ``platform_name``: aquery's ``platformName`` of the configuration. Bazel 8 derives it
  from ``--cpu``, so under ``--platforms`` it can name the host cpu for an x86_64 target
  configuration; the compile's ``-target`` is the truth;
- ``execution_platform``: the platform Bazel resolved to run the action;
- ``tool_config``: the role of the configuration the main step's program was built in,
  when the program is an output of the graph (a flatc built for exec, run by a
  target-configuration genrule, has ``tool_config: exec``);
- ``target_rule``: the rule class of the owning target (cc_library, genrule, ...);
- ``mnemonic``: Bazel's action mnemonic (also ``rule``, the Ninja rule's slot).

Paths: ``bazel-out/<mnemonic>`` becomes ``$OUT_<role>`` (``$OUT_exec``,
``$OUT_target``, ``$OUT_target:darwin_x86_64``); Xcode's placeholders become ``$SDK``,
``$DEVELOPER_DIR`` and ``$EXECROOT``; ``--map`` rewrites external repositories
(``--map 'external/flatbuffers+=$SRC'``); ``--main`` prefixes the main repository's
source files. Inputs keep sources, schemas, objects and archives (headers are
counted, not listed) and drop the C++ toolchain's own files.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from collections import Counter, defaultdict

# Bazel housekeeping: actions a Ninja build has no counterpart for.
HOUSEKEEPING = {
    "Symlink": "bazel: header symlink (_virtual_includes)",
    "CppModuleMap": "bazel: C++ module map (written when the compiler is clang)",
    "SolibSymlink": "bazel: shared-library symlink (_solib)",
    "SymlinkTree": "bazel: runfiles tree",
    "SourceSymlinkManifest": "bazel: runfiles manifest",
    "RepoMappingManifest": "bazel: repo mapping manifest",
    "FileWrite": "bazel: file write",
    "Middleman": "bazel: middleman",
    "TranslateBuildInfo": "bazel: build info",
    "TemplateExpand": "bazel: template expansion",
    "ExecutableSymlink": "bazel: executable symlink",
    "CcStrip": "bazel: strip (the .stripped output, built only on request)",
}
KINDS = {"CppCompile": "compile", "CppLink": "link", "CppArchive": "archive", "Genrule": "custom"}
TOOLCHAIN_INPUT = re.compile(
    r"^(?:bazel-out/[^/]+/bin/)?external/(?:apple_support\+|bazel_tools|rules_cc\+\+cc_configure"
    r"|rules_cc\+/cc/private/toolchain|[^/]*local_config_)"
    r"|^bazel-out/[^/]+/internal/_middlemen/"
)
HEADER = re.compile(r"\.(?:h|hh|hpp|hxx|inc|ipp|inl)$")
XCODE = [
    ("__BAZEL_XCODE_SDKROOT__", "$SDK"),
    ("__BAZEL_XCODE_DEVELOPER_DIR__", "$DEVELOPER_DIR"),
    ("__BAZEL_EXECUTION_ROOT__", "$EXECROOT"),
]
SHELL_SEPARATORS = {";", "&&", "||", "|", "done", "do", "then", "fi"}


class Graph:
    """The aquery jsonproto tables, indexed."""

    def __init__(self, data: dict):
        self.frag = {f["id"]: f for f in data.get("pathFragments", [])}
        self._paths: dict[int, str] = {}
        self.artifact = {
            a["id"]: self._path(a["pathFragmentId"]) for a in data.get("artifacts", [])
        }
        self.depset = {d["id"]: d for d in data.get("depSetOfFiles", [])}
        self.rule_class = {r["id"]: r["name"] for r in data.get("ruleClasses", [])}
        self.target = {t["id"]: t for t in data.get("targets", [])}
        self.config = {c["id"]: c for c in data.get("configuration", [])}
        self.actions = data.get("actions", [])

    def _path(self, fid: int) -> str:
        if fid not in self._paths:
            f = self.frag[fid]
            parent = f.get("parentId")
            self._paths[fid] = (
                f["label"] if parent is None else self._path(parent) + "/" + f["label"]
            )
        return self._paths[fid]

    def inputs(self, action: dict) -> list[str]:
        seen: set[int] = set()
        out: list[str] = []
        stack = list(reversed(action.get("inputDepSetIds", [])))
        while stack:
            d = self.depset[stack.pop()]
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            out.extend(self.artifact[a] for a in d.get("directArtifactIds", []))
            stack.extend(reversed(d.get("transitiveDepSetIds", [])))
        return out


def config_roles(graph: Graph) -> dict[int, str]:
    """Configuration id -> role name, unique per configuration."""
    base = {cid: ("exec" if c.get("isTool") else "target") for cid, c in graph.config.items()}
    count = Counter(base.values())
    role = {}
    for cid, c in graph.config.items():
        r = base[cid]
        if count[r] > 1:
            r = f"{r}:{c.get('platformName', cid)}"
        role[cid] = r
    count = Counter(role.values())
    return {cid: (f"{r}:{cid}" if count[r] > 1 else r) for cid, r in role.items()}


def normalize_label(label: str) -> str:
    """@@flatbuffers+//src:flatc -> @flatbuffers//src:flatc (the canonical name's module part)."""
    m = re.match(r"^@@([^/+]*)\+*[^/]*//", label)
    if m:
        return "@" + m.group(1) + label[label.index("//") :]
    return label


class Normalizer:
    def __init__(self, graph: Graph, maps: list[tuple[str, str]], main: str | None):
        self.graph = graph
        self.role = config_roles(graph)
        subs = [
            (f"bazel-out/{c['mnemonic']}", f"$OUT_{self.role[cid]}")
            for cid, c in graph.config.items()
        ]
        subs += XCODE
        self.subs = sorted(subs, key=lambda s: -len(s[0]))
        # A mapped repository is its *source* tree: the prefix only where a path
        # starts, not inside an output dir ($OUT_exec/bin/external/<repo>/...).
        self.maps = [
            (re.compile(rf"(?<![\w/.+-]){re.escape(old)}(?=/|$|[^\w.+-])"), new)
            for old, new in sorted(maps, key=lambda m: -len(m[0]))
        ]
        self.main = main
        self.main_sources = {
            p
            for p in graph.artifact.values()
            if main and not p.startswith(("bazel-out/", "external/"))
        }
        # Built files -> the role of the configuration that built them.
        self.built: dict[str, str] = {}
        # Linked programs and libraries (what a command can run or load).
        self.linked: set[str] = set()
        for a in graph.actions:
            for oid in a.get("outputIds", []):
                self.built[graph.artifact[oid]] = self.role[a["configurationId"]]
            if a["mnemonic"] == "CppLink" and "primaryOutputId" in a:
                self.linked.add(graph.artifact[a["primaryOutputId"]])

    def path(self, p: str) -> str:
        if p in self.main_sources:
            return f"{self.main}/{p}"
        return self.text(p)

    def text(self, s: str) -> str:
        for old, new in self.subs:
            s = s.replace(old, new)
        for rx, new in self.maps:
            s = rx.sub(lambda _m, new=new: new, s)
        return s

    def token(self, t: str) -> str:
        return self.path(t) if t in self.main_sources else self.text(t)

    def script(self, s: str) -> str:
        """A shell script: rewrite paths, and main-repo sources where they stand as words."""
        s = self.text(s)
        for p in sorted(self.main_sources, key=len, reverse=True):
            s = re.sub(rf"(?<![\w./$-]){re.escape(p)}(?![\w./-])", f"{self.main}/{p}", s)
        return s

    def record(self, a: dict, variant: str | None, excludes: list[tuple[re.Pattern, str]]) -> dict:
        g = self.graph
        tgt = g.target[a["targetId"]]
        label = normalize_label(tgt["label"])
        cfg = g.config[a["configurationId"]]
        mnemonic = a["mnemonic"]
        outputs = sorted(self.path(g.artifact[o]) for o in a.get("outputIds", []))
        primary = a.get("primaryOutputId")
        rec: dict = {
            "id": self.path(g.artifact[primary]) if primary else outputs[0],
            "outputs": outputs,
            "kind": KINDS.get(mnemonic, "meta" if mnemonic in HOUSEKEEPING else "custom"),
            "mnemonic": mnemonic,
            "target": label,
            "target_rule": g.rule_class.get(tgt.get("ruleClassId"), ""),
            "configuration": cfg["mnemonic"],
            "config_role": self.role[a["configurationId"]],
            "platform_name": cfg.get("platformName", ""),
        }
        if variant:
            rec["variant"] = variant
        reason = HOUSEKEEPING.get(mnemonic)
        if reason is None:
            reason = next((r for rx, r in excludes if rx.search(label)), None)
        if reason is not None:
            rec["excluded"] = reason
            return rec

        raw_inputs = g.inputs(a)
        kept = sorted(
            {
                self.path(p)
                for p in raw_inputs
                if not TOOLCHAIN_INPUT.match(p) and not HEADER.search(p)
            }
        )
        rec["inputs"] = {"explicit": kept, "implicit": [], "order_only": []}
        rec["header_inputs"] = sum(
            1 for p in raw_inputs if HEADER.search(p) and not TOOLCHAIN_INPUT.match(p)
        )
        rec["rule"] = mnemonic
        rec["phony"] = False
        rec["origin"] = "rule"
        rec["execution_platform"] = normalize_label(a.get("executionPlatform", ""))
        env = {e["key"]: e["value"] for e in a.get("environmentVariables", [])}
        if env:
            rec["env"] = env

        argv = a.get("arguments", [])
        steps, main = self.steps(argv)
        rec["steps"], rec["main"] = steps, main
        if len(steps) > 1 or steps[main]["argv"] != [self.token(t) for t in argv]:
            rec["argv"] = [
                self.script(t) if i == 2 and mnemonic == "Genrule" else self.token(t)
                for i, t in enumerate(argv)
            ]
        if mnemonic == "CppLink":
            rec["link_type"] = (
                "shared"
                if ("-dynamiclib" in argv or "-shared" in argv)
                else "module"
                if "-bundle" in argv
                else "executable"
            )
        tool_raw = steps[main]["raw_tool"]
        rec["tool"] = steps[main]["argv"][0]
        rec["tool_name"] = os.path.basename(tool_raw)
        rec["tool_built"] = tool_raw in self.built
        if tool_raw in self.built:
            rec["tool_config"] = self.built[tool_raw]
        own = {g.artifact[o] for o in a.get("outputIds", [])}
        runs = sorted(
            self.path(t) for t in steps[main]["raw_argv"][1:] if t in self.linked and t not in own
        )
        if runs:
            rec["runs_built"] = runs
        for s in steps:
            del s["raw_tool"], s["raw_argv"]
        return rec

    def steps(self, argv: list[str]) -> tuple[list[dict], int]:
        """Decompose a command. A ``bash -c`` script (a genrule) is split at shell
        separators; the main step is the one whose program is a built executable,
        with its leading VAR=value words as ``env``. Other commands are one step."""
        if len(argv) == 3 and os.path.basename(argv[0]) in ("bash", "sh") and argv[1] == "-c":
            lex = shlex.shlex(argv[2], posix=True, punctuation_chars=";&|")
            lex.whitespace_split = True
            try:
                words = list(lex)
            except ValueError:
                words = []
            segments: list[list[str]] = [[]]
            for w in words:
                if w in SHELL_SEPARATORS:
                    segments.append([])
                else:
                    segments[-1].append(w)
            steps: list[dict] = []
            main = None
            for seg in segments:
                env = {}
                while seg and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
                    k, _, v = seg.pop(0).partition("=")
                    env[k] = self.script(v)
                if not seg:
                    continue
                step = {
                    "argv": [self.token(w) for w in seg],
                    "cwd": "$EXECROOT",
                    "raw_tool": seg[0],
                    "raw_argv": seg,
                }
                if env:
                    step["env"] = env
                if main is None and seg[0] in self.built:
                    main = len(steps)
                steps.append(step)
            if steps:
                return steps, (main if main is not None else len(steps) - 1)
        step = {
            "argv": [self.token(t) for t in argv],
            "cwd": "$EXECROOT",
            "raw_tool": argv[0],
            "raw_argv": argv,
        }
        return [step], 0


def parse_pairs(items: list[str], what: str) -> list[tuple[str, str]]:
    out = []
    for item in items:
        k, sep, v = item.partition("=")
        if not sep:
            sys.exit(f"{what}: expected KEY=VALUE, got {item!r}")
        out.append((k, v))
    return out


def normalize(
    data: dict,
    variant: str | None,
    maps: list[tuple[str, str]],
    main: str | None,
    excludes: list[tuple[str, str]],
) -> list[dict]:
    graph = Graph(data)
    norm = Normalizer(graph, maps, main)
    rx = [(re.compile(p), r) for p, r in excludes]
    records = [norm.record(a, variant, rx) for a in graph.actions]
    records.sort(key=lambda r: (r["id"], r["config_role"], r["mnemonic"]))
    return records


def summary(records: list[dict]) -> str:
    """Counts per (config_role, kind), in-scope records only, and the sources compiled
    in more than one configuration."""
    lines = []
    configs = sorted({(r["config_role"], r["configuration"], r["platform_name"]) for r in records})
    lines.append("configurations:")
    lines += [f"  {role:<24} {mn}  (platformName {p})" for role, mn, p in configs]
    counts = Counter((r["config_role"], r["kind"]) for r in records if "excluded" not in r)
    lines.append("in-scope actions (role, kind: count):")
    lines += [f"  {role:<24} {kind:<8} {n}" for (role, kind), n in sorted(counts.items())]
    excl = Counter(r["excluded"] for r in records if "excluded" in r)
    lines.append("excluded:")
    lines += [f"  {n:>4}  {reason}" for reason, n in sorted(excl.items())]
    by_src = defaultdict(set)
    for r in records:
        if r.get("kind") == "compile" and "excluded" not in r:
            argv = r["steps"][r["main"]]["argv"]
            if "-c" in argv:
                by_src[argv[argv.index("-c") + 1]].add(r["config_role"])
    multi = sorted(s for s, roles in by_src.items() if len(roles) > 1)
    lines.append(f"sources compiled in more than one configuration: {len(multi)}")
    lines += [f"  {s}  ({', '.join(sorted(by_src[s]))})" for s in multi]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("normalize", help="aquery jsonproto -> JSONL")
    n.add_argument("aquery")
    n.add_argument("-o", "--output", required=True)
    n.add_argument("--variant")
    n.add_argument("--map", action="append", default=[], metavar="PREFIX=PLACEHOLDER")
    n.add_argument("--main", metavar="PLACEHOLDER", help="prefix for the main repository's sources")
    n.add_argument("--exclude-label", action="append", default=[], metavar="REGEX=REASON")
    s = sub.add_parser("summary", help="counts per configuration role and kind")
    s.add_argument("jsonl")
    args = ap.parse_args(argv)
    if args.cmd == "normalize":
        with open(args.aquery) as f:
            data = json.load(f)
        records = normalize(
            data,
            args.variant,
            parse_pairs(args.map, "--map"),
            args.main,
            parse_pairs(args.exclude_label, "--exclude-label"),
        )
        with open(args.output, "w") as f:
            f.writelines(
                json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records
            )
        print(summary(records), end="")
        return 0
    with open(args.jsonl) as f:
        records = [json.loads(line) for line in f if line.strip()]
    print(summary(records), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
