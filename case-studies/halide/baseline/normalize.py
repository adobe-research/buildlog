# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Normalize a Ninja build graph into canonical, machine-independent records.

The case study compares Halide's own CMake build with a buildlog build of the same
project. Both end up as Ninja files, so both are normalized the same way: one JSON
record per build edge, with machine paths replaced by placeholders, commands split
into tokens and decomposed into steps, and each record tagged with its CMake target
(from the File API, when there is one), its kind and its case-study phase.

  normalize.py normalize --build-ninja FILE --build DIR --src DIR [--fileapi DIR]
                         [--llvm DIR] [--scope scope.toml] [--variant NAME] -o OUT.jsonl
  normalize.py install-manifest --prefix DIR [--fileapi DIR] --src DIR --build DIR -o OUT
  normalize.py test-regex --scope scope.toml

See case-studies/halide/README.md for the record format. Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import ClassVar

# ---------------------------------------------------------------------------
# Ninja parsing


@dataclass
class Edge:
    outputs: list[str]
    implicit_outputs: list[str]
    rule: str
    explicit: list[str]
    implicit: list[str]
    order_only: list[str]
    bindings: dict[str, str]
    section_target: str | None = None
    line: int = 0


@dataclass
class NinjaFile:
    rules: dict[str, dict[str, list]] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    scope: dict[str, str] = field(default_factory=dict)
    defaults: list[str] = field(default_factory=list)


_VAR_SIMPLE = re.compile(r"[A-Za-z0-9_-]+")


def parse_eval(s: str, split_paths: bool = False) -> list:
    """Parse a Ninja value into pieces [(is_var, text)].

    With split_paths, returns a list of tokens: each token is either a separator
    string ('|', '||', '|@', ':') or a list of pieces (one path)."""
    tokens: list = []
    cur: list = []
    lit: list[str] = []
    started = False

    def flush_lit():
        if lit:
            cur.append((False, "".join(lit)))
            lit.clear()

    def end_token():
        nonlocal cur, started
        flush_lit()
        if started:
            tokens.append(cur)
        cur = []
        started = False

    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "$" and i + 1 < n:
            nx = s[i + 1]
            if nx in " :$":
                lit.append(nx)
                i += 2
            elif nx == "\n":
                i += 2
                while i < n and s[i] == " ":
                    i += 1
            elif nx == "{":
                j = s.index("}", i)
                flush_lit()
                cur.append((True, s[i + 2 : j]))
                i = j + 1
            else:
                m = _VAR_SIMPLE.match(s, i + 1)
                if not m:
                    raise ValueError(f"bad $-escape in {s!r}")
                flush_lit()
                cur.append((True, m.group(0)))
                i = m.end()
            started = True
            continue
        if split_paths:
            if c == " ":
                end_token()
                i += 1
                continue
            if c == "|" and not started:
                if s.startswith("||", i):
                    tokens.append("||")
                    i += 2
                elif s.startswith("|@", i):
                    tokens.append("|@")
                    i += 2
                else:
                    tokens.append("|")
                    i += 1
                continue
            if c == ":":
                end_token()
                tokens.append(":")
                i += 1
                continue
        lit.append(c)
        started = True
        i += 1
    if split_paths:
        end_token()
        return tokens
    flush_lit()
    return cur


def evaluate(pieces: list, lookup) -> str:
    return "".join(lookup(t) if is_var else t for is_var, t in pieces)


def _logical_lines(text: str):
    """Yield (lineno, line) with $-continuations joined; comments are kept."""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        start = i
        line = lines[i]
        while _ends_with_escape(line) and i + 1 < len(lines):
            i += 1
            line = line[:-1] + "$\n" + lines[i]
        yield start + 1, line
        i += 1


def _ends_with_escape(line: str) -> bool:
    k = len(line) - len(line.rstrip("$"))
    return k % 2 == 1


_SECTION = [
    re.compile(r"^# (?:Object|Link) build statements for \S+ target (\S+)$"),
    re.compile(r"^# Utility command for (\S+)$"),
    re.compile(r"^# Object library (\S+)$"),
    re.compile(r"^# Order-only phony target for (\S+)$"),
]
# Comments inside a target's section that do not end it.
_SECTION_KEEP = (
    "# Custom command for",
    "# Phony custom command for",
    "# Link the ",
    "# Create library symlink",
)


def parse_ninja(path: str, nf: NinjaFile | None = None) -> NinjaFile:
    nf = nf or NinjaFile()
    base = os.path.dirname(path)
    with open(path) as f:
        text = f.read()
    section: str | None = None
    current = None  # ('rule', name) | ('build', Edge) | ('pool',)
    pending_build = None

    def file_lookup(v):
        return nf.scope.get(v, "")

    def finish_build():
        nonlocal pending_build
        if pending_build is None:
            return
        toks, bindings_raw, lineno = pending_build
        edge_env = {}
        for k, v in bindings_raw:
            edge_env[k] = evaluate(v, lambda x: edge_env.get(x, file_lookup(x)))
        groups = {"out": [], "iout": [], "in": [], "imp": [], "oo": [], "val": []}
        mode = "out"
        rule = None
        for t in toks:
            if t == ":":
                mode = "rule"
                continue
            if t == "|":
                mode = "iout" if mode == "out" else "imp"
                continue
            if t == "||":
                mode = "oo"
                continue
            if t == "|@":
                mode = "val"
                continue
            val = evaluate(t, file_lookup)
            if mode == "rule":
                rule = val
                mode = "in"
            else:
                groups[mode].append(val)
        nf.edges.append(
            Edge(
                outputs=groups["out"],
                implicit_outputs=groups["iout"],
                rule=rule or "",
                explicit=groups["in"],
                implicit=groups["imp"],
                order_only=groups["oo"],
                bindings=edge_env,
                section_target=section,
                line=lineno,
            )
        )
        pending_build = None

    for lineno, line in _logical_lines(text):
        stripped = line.lstrip(" ")
        if not stripped:
            continue
        if stripped.startswith("#"):
            finish_build()
            current = None
            for rx in _SECTION:
                m = rx.match(stripped)
                if m:
                    section = m.group(1)
                    break
            else:
                if not stripped.startswith(_SECTION_KEEP) and not stripped.startswith("####"):
                    section = None
            continue
        indented = line.startswith(" ")
        if indented and current is not None:
            k, _, v = stripped.partition("=")
            k, v = k.strip(), parse_eval(v.lstrip(" "))
            if current[0] == "rule":
                nf.rules[current[1]][k] = v
            elif current[0] == "build":
                pending_build[1].append((k, v))
            continue
        finish_build()
        current = None
        if stripped.startswith("rule "):
            name = stripped[5:].strip()
            nf.rules[name] = {}
            current = ("rule", name)
        elif stripped.startswith("build "):
            pending_build = (parse_eval(stripped[6:], split_paths=True), [], lineno)
            current = ("build",)
        elif stripped.startswith("pool "):
            current = ("pool",)
        elif stripped.startswith("default "):
            for t in parse_eval(stripped[8:], split_paths=True):
                nf.defaults.append(evaluate(t, file_lookup))
        elif stripped.startswith(("include ", "subninja ")):
            rel = evaluate(parse_eval(stripped.split(" ", 1)[1].strip()), file_lookup)
            finish_build()
            parse_ninja(os.path.join(base, rel), nf)
        else:
            k, _, v = stripped.partition("=")
            nf.scope[k.strip()] = evaluate(parse_eval(v.lstrip(" ")), file_lookup)
    finish_build()
    return nf


_SHELL_SAFE = re.compile(r"^[A-Za-z0-9_+,\-./@]+$")


def _shell_escape(p: str) -> str:
    return p if _SHELL_SAFE.match(p) else "'" + p.replace("'", "'\\''") + "'"


def edge_var(nf: NinjaFile, e: Edge, name: str, _depth: int = 0) -> str:
    """Ninja's lookup order: $in/$out, edge bindings, rule bindings, file scope."""
    if name == "in":
        return " ".join(_shell_escape(p) for p in e.explicit)
    if name == "in_newline":
        return "\n".join(_shell_escape(p) for p in e.explicit)
    if name == "out":
        return " ".join(_shell_escape(p) for p in e.outputs)
    if name in e.bindings:
        return e.bindings[name]
    rule = nf.rules.get(e.rule, {})
    if name in rule and _depth < 20:
        return evaluate(rule[name], lambda v: edge_var(nf, e, v, _depth + 1))
    return nf.scope.get(name, "")


# ---------------------------------------------------------------------------
# Machine paths -> placeholders


class Roots:
    """Longest-prefix substitution of machine roots by placeholders."""

    PATTERNS: ClassVar[list[tuple[str, str]]] = [
        ("$SDK", r"/Applications/Xcode[^/]*\.app/Contents/Developer/Platforms/[^/]+/Developer/SDKs/[^/]+\.sdk"),
        ("$SDK", r"/Library/Developer/CommandLineTools/SDKs/[^/]+\.sdk"),
        ("$TOOLCHAIN", r"/Applications/Xcode[^/]*\.app/Contents/Developer/Toolchains/[^/]+\.xctoolchain"),
        ("$TOOLCHAIN", r"/Library/Developer/CommandLineTools"),
        ("$DEVELOPER", r"/Applications/Xcode[^/]*\.app/Contents/Developer"),
    ]  # fmt: skip

    def __init__(self, roots: dict[str, list[str]]):
        literal = []
        for ph, paths in roots.items():
            for p in paths:
                if p:
                    p = p.rstrip("/")
                    literal.append((ph, p))
                    rp = os.path.realpath(p)
                    if rp != p:
                        literal.append((ph, rp))
        literal.sort(key=lambda t: -len(t[1]))
        self.literal = literal
        alts = [f"(?P<L{i}>{re.escape(p)})" for i, (_, p) in enumerate(literal)]
        alts += [f"(?P<R{i}>{rx})" for i, (_, rx) in enumerate(self.PATTERNS)]
        # The rest of a matched path is kept as is: a short root such as the Linux
        # container's "/src" must not match again inside "$BUILD/src/...".
        self.rx = re.compile(
            "(?:" + "|".join(alts) + r")(?![A-Za-z0-9_.@+\-])(?P<rest>(?:/[A-Za-z0-9_.@+\-]*)*)"
        )
        self.build = next((p for ph, p in literal if ph == "$BUILD"), None)

    def sub(self, s: str) -> str:
        def repl(m):
            k = next(g for g, v in m.groupdict().items() if v is not None and g != "rest")
            ph = self.literal[int(k[1:])][0] if k.startswith("L") else self.PATTERNS[int(k[1:])][0]
            return ph + m.group("rest")

        return self.rx.sub(repl, s)


_HASHED_DEPFILE = re.compile(r"(CMakeFiles/d/)[0-9a-f]{64}(\.d)\b")


def canon_str(roots: Roots, s: str) -> str:
    return _HASHED_DEPFILE.sub(r"\1<sha256>\2", roots.sub(s))


# ---------------------------------------------------------------------------
# Command decomposition

_SEPARATORS = {"&&", "||", ";"}
_REDIRECTS = {"<": "stdin", ">": "stdout", ">>": "stdout_append", "2>": "stderr"}
# Compiler launchers (CMake's <LANG>_COMPILER_LAUNCHER, Meson's automatic ccache).
_LAUNCHERS = {"ccache", "sccache", "distcc"}
_MESON = {"meson", "meson.py"}
_MESON_EXE_OPTS = {"--capture": "stdout", "--feed": "stdin", "--chdir": "cwd"}


def split_steps(tokens: list[str]) -> list[list[str]]:
    steps, cur = [], []
    for t in tokens:
        if t in _SEPARATORS:
            steps.append(cur)
            cur = []
        else:
            cur.append(t)
    steps.append(cur)
    return [s for s in steps if s]


def decompose(command: str, cwd: str) -> list[dict]:
    """Split a shell command into steps: {argv, cwd, stdin?, stdout?, env?, wrapper?}.

    Handles `cd DIR &&` prefixes, `:` no-ops, redirections, `/bin/sh -c "..."`,
    `cmake -E env K=V ... cmd` and `meson --internal exe ... -- cmd` wrappers and
    compiler launchers (ccache). Paths are left as written."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return [{"argv": command.split(), "cwd": cwd, "unparsed": True}]
    out = []
    for st in split_steps(tokens):
        if st == [":"]:
            continue
        if st[0] == "cd" and len(st) == 2:
            cwd = os.path.normpath(os.path.join(cwd, st[1]))
            continue
        step: dict = {"cwd": cwd}
        argv = []
        it = iter(range(len(st)))
        for i in it:
            t = st[i]
            if t in _REDIRECTS and i + 1 < len(st):
                step[_REDIRECTS[t]] = st[i + 1]
                next(it, None)
            elif t == "2>&1":
                step["stderr"] = "&1"
            else:
                argv.append(t)
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        if prog in ("sh", "bash", "zsh") and len(argv) >= 3 and argv[1] == "-c":
            inner = decompose(argv[2], cwd)
            for s in inner:
                s["wrapper"] = argv[:2]
            out.extend(inner)
            continue
        if prog == "cmake" and argv[1:3] == ["-E", "env"]:
            env, j = {}, 3
            while j < len(argv) and (
                "=" in argv[j] or argv[j].startswith("--unset") or argv[j] == "--modify"
            ):
                k, _, v = argv[j].partition("=")
                env[k] = v
                j += 1
            if j < len(argv) and argv[j] == "--":
                j += 1
            step["wrapper"] = argv[:3]
            step["env"] = env
            argv = argv[j:]
        elif prog in _MESON and argv[1:3] == ["--internal", "exe"]:
            # Meson's `meson --internal exe [--capture F] [--feed F] [--chdir D]
            # [--] argv`: run argv with stdout to F, stdin from F, in D. The
            # pickled form (--unpickle) is left as it is.
            j = 3
            while j + 1 < len(argv) and argv[j] in _MESON_EXE_OPTS:
                opt, val = argv[j], argv[j + 1]
                if opt == "--chdir":
                    cwd = os.path.normpath(os.path.join(cwd, val))
                    step["cwd"] = cwd
                else:
                    step[_MESON_EXE_OPTS[opt]] = val
                j += 2
            if j < len(argv) and argv[j] == "--":
                j += 1
            if j < len(argv) and argv[j] != "--unpickle":
                step["wrapper"] = argv[:3]
                argv = argv[j:]
        if os.path.basename(argv[0]) in _LAUNCHERS and len(argv) > 1:
            step["wrapper"] = step.get("wrapper", []) + argv[:1]
            argv = argv[1:]
        step["argv"] = argv
        out.append(step)
    return out


_CMAKE_FS_OPS = {
    "rm",
    "remove",
    "remove_directory",
    "touch",
    "touch_nocreate",
    "make_directory",
    "cmake_transform_depfile",
    "copy",
    "copy_if_different",
    "copy_directory",
    "copy_directory_if_different",
    "create_symlink",
    "create_hardlink",
    "rename",
    "echo",
    "echo_append",
    "true",
}
_CMAKE_COPY_OPS = {
    "copy",
    "copy_if_different",
    "copy_directory",
    "copy_directory_if_different",
    "create_symlink",
    "create_hardlink",
    "rename",
}
_COMPILER = re.compile(r"^(?:.*-)?(?:cc|c\+\+|clang|clang\+\+|gcc|g\+\+)(?:-\d+(?:\.\d+)*)?$")


def _cmake_op(argv: list[str]) -> str | None:
    """The file operation of `cmake -E op ...`, or of buildlog's portable
    equivalent `buildlog -E op ...` (spelled `python -m buildlog -E op ...`)."""
    if os.path.basename(argv[0]) in ("cmake", "buildlog") and len(argv) > 2 and argv[1] == "-E":
        return argv[2]
    if argv[1:4] == ["-m", "buildlog", "-E"] and len(argv) > 4:
        return argv[4]
    return None


def main_step_index(steps: list[dict]) -> int | None:
    """The step that does the work: the first that is not a cmake -E file operation;
    if all are, the first copy-like one, else the first step."""
    if not steps:
        return None
    for i, s in enumerate(steps):
        op = _cmake_op(s["argv"])
        if op is None or op not in _CMAKE_FS_OPS:
            return i
    for i, s in enumerate(steps):
        if _cmake_op(s["argv"]) in _CMAKE_COPY_OPS:
            return i
    return 0


def classify(rule: str, steps: list[dict], main: int | None, generator: bool) -> tuple[str, str]:
    """(kind, link_type). The kind is derived from the command, not CMake's rule names,
    so buildlog graphs classify the same way (rule names only refine link_type)."""
    if rule == "phony":
        return "phony", ""
    if generator:
        return "regen", ""
    if main is None:
        return "stamp", ""
    argv = steps[main]["argv"]
    prog = os.path.basename(argv[0])
    link_type = ""
    if _COMPILER.match(prog):
        # Clang spells a PCH build -Xclang -emit-pch; GCC just compiles a header (-x
        # c++-header) into a .gch.
        if "-emit-pch" in argv or any(
            a == "-x" and b in ("c-header", "c++-header") for a, b in itertools.pairwise(argv)
        ):
            return "pch", ""
        if "-c" in argv or "-S" in argv or "-E" in argv:
            return "compile", ""
        if "_MODULE_LIBRARY_LINKER" in rule or "-bundle" in argv:
            link_type = "module"
        elif "_SHARED_LIBRARY_LINKER" in rule or "-dynamiclib" in argv or "-shared" in argv:
            link_type = "shared"
        else:
            link_type = "executable"
        return "link", link_type
    if prog in ("ar", "libtool", "llvm-ar") or prog.endswith("-ar"):
        return "archive", ""
    if prog == "lipo":
        return "lipo", ""
    op = _cmake_op(argv)
    if op == "cmake_symlink_library":
        return "symlink", ""
    if prog == "ln" and any(a.startswith("-") and "s" in a for a in argv[1:]):
        return "symlink", ""
    if op is not None:
        ops = {_cmake_op(s["argv"]) for s in steps}
        if None not in ops and ops <= _CMAKE_FS_OPS:
            return ("copy" if ops & _CMAKE_COPY_OPS else "stamp"), ""
    if prog == "cmake":
        if "-P" in argv and any(a.endswith("cmake_install.cmake") for a in argv):
            return "install", ""
        if "--regenerate-during-build" in argv:
            return "meta", ""
    if prog in ("cpack", "ctest", "ccmake", "cmake-gui", "ninja"):
        return "meta", ""
    if prog in _MESON:  # `meson --internal exe` runs are unwrapped by decompose
        return "meta", ""
    return "custom", ""


# ---------------------------------------------------------------------------
# CMake File API


@dataclass
class FTarget:
    name: str
    type: str
    dir: str
    build_dir: str
    objdir: str
    artifacts: list[str]
    sources: list[str]
    generated: list[str]
    deps: list[str]
    defined_in: str | None


def load_fileapi(reply: str, src: str, build: str) -> dict[str, FTarget]:
    idx = sorted(glob.glob(os.path.join(reply, "index-*.json")))
    if not idx:
        raise SystemExit(f"no File API index in {reply}")
    with open(idx[-1]) as f:
        index = json.load(f)
    cm = None
    for r in index["reply"].values():
        if isinstance(r, dict) and r.get("kind") == "codemodel":
            cm = r["jsonFile"]
    if cm is None:
        for resp in (
            index["reply"].get("client-buildlog", {}).get("query.json", {}).get("responses", [])
        ):
            if resp.get("kind") == "codemodel":
                cm = resp["jsonFile"]
    with open(os.path.join(reply, cm)) as f:
        codemodel = json.load(f)
    top_src = codemodel["paths"]["source"]
    top_build = codemodel["paths"]["build"]

    def abs_src(p):
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(top_src, p))

    def abs_build(p):
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(top_build, p))

    def remap(p):  # the File API was produced with the real src/build roots
        if p.startswith(top_build):
            return os.path.normpath(build + p[len(top_build) :])
        if p.startswith(top_src):
            return os.path.normpath(src + p[len(top_src) :])
        return p

    conf = codemodel["configurations"][0]
    dirs = conf["directories"]
    ids: dict[str, str] = {}
    raw = []
    for t in conf["targets"]:
        with open(os.path.join(reply, t["jsonFile"])) as f:
            tj = json.load(f)
        ids[tj["id"]] = tj["name"]
        raw.append((t, tj))
    out: dict[str, FTarget] = {}
    for t, tj in raw:
        d = dirs[t["directoryIndex"]]
        bdir = tj["paths"]["build"]
        defined = None
        bt = tj.get("backtrace")
        g = tj.get("backtraceGraph")
        if bt is not None and g:
            defined = g["files"][g["nodes"][bt]["file"]]
        srcs = [remap(abs_src(s["path"])) for s in tj.get("sources", [])]
        gen = [remap(abs_src(s["path"])) for s in tj.get("sources", []) if s.get("isGenerated")]
        out[tj["name"]] = FTarget(
            name=tj["name"],
            type=tj["type"],
            dir=d["source"],
            build_dir=bdir,
            objdir=remap(abs_build(os.path.join(bdir, "CMakeFiles", tj["name"] + ".dir"))),
            artifacts=[remap(abs_build(a["path"])) for a in tj.get("artifacts", [])],
            sources=srcs,
            generated=gen,
            deps=sorted(ids[x["id"]] for x in tj.get("dependencies", []) if x["id"] in ids),
            defined_in=defined,
        )
    return out


# ---------------------------------------------------------------------------
# Scope rules


def _braces(pat: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pat)
    if not m:
        return [pat]
    out = []
    for alt in m.group(1).split(","):
        out += _braces(pat[: m.start()] + alt + pat[m.end() :])
    return out


def gmatch(value: str | None, pat: str) -> bool:
    if value is None:
        return False
    return any(fnmatch.fnmatchcase(value, p) for p in _braces(pat))


def load_scope(path: str | None) -> dict:
    if not path:
        return {"rule": [], "tests": {}}
    with open(path, "rb") as f:
        return tomllib.load(f)


def sampled_tests(scope: dict) -> list[str]:
    t = scope.get("tests", {})
    return list(t.get("correctness", [])) + list(t.get("generator", []))


def sample_closure(scope: dict, targets: dict[str, FTarget]) -> set[str]:
    overrides = scope.get("tests", {}).get("targets", {})
    todo = [overrides.get(n, n) for n in sampled_tests(scope)]
    missing = [n for n in todo if n not in targets]
    if missing and targets:
        print(f"warning: sampled test targets not in File API: {missing}", file=sys.stderr)
    seen: set[str] = set()
    while todo:
        n = todo.pop()
        if n in seen or n not in targets:
            continue
        seen.add(n)
        todo += targets[n].deps
    return seen


def apply_rules(rec: dict, rules: list[dict], closure: set[str]) -> tuple[int | None, str | None]:
    outs = rec["outputs"] + rec.get("implicit_outputs", [])
    for r in rules:
        ok = True
        for k, v in r.items():
            if k in ("phase", "exclude", "why"):
                continue
            if k == "target":
                ok = gmatch(rec.get("target"), v)
            elif k == "dir":
                ok = gmatch(rec.get("dir"), v)
            elif k == "kind":
                ok = gmatch(rec.get("kind"), v)
            elif k == "tool":
                ok = gmatch(rec.get("tool_name"), v)
            elif k == "output":
                ok = any(gmatch(o, v) for o in outs)
            elif k == "sampled":
                ok = (rec.get("target") in closure) == bool(v)
            elif k == "no_target":
                ok = (rec.get("target") is None) == bool(v)
            else:
                raise SystemExit(f"unknown scope rule key {k!r} in {r}")
            if not ok:
                break
        if ok:
            if "exclude" in r:
                return None, r["exclude"]
            return int(r["phase"]), None
    return None, "unmapped"


# ---------------------------------------------------------------------------
# Normalization


_PATHLIKE = re.compile(r"^[\w.+@\-]+(?:/[\w.+@\-]*)*$")
_EXT = re.compile(r"[^/.]\.[A-Za-z]\w*$")  # a file name with a stem and an extension
_PATH_FLAGS = ("-isystem", "-iquote", "-idirafter", "-I", "-F", "-L")


class Normalizer:
    def __init__(self, nf: NinjaFile, build: str, roots: Roots):
        self.nf = nf
        self.build = os.path.normpath(build)
        self.roots = roots
        self.known: set[str] = set()
        for e in nf.edges:
            for p in e.outputs + e.implicit_outputs + e.explicit + e.implicit + e.order_only:
                self.known.add(self.absp(p))

    def absp(self, p: str, cwd: str | None = None) -> str:
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd or self.build, p))

    def cpath(self, p: str, cwd: str | None = None) -> str:
        a = self.absp(p, cwd)
        return canon_str(self.roots, a)

    def ctoken(self, t: str, cwd: str) -> str:
        for flag in _PATH_FLAGS:  # an attached search path, e.g. -I../src
            if t.startswith(flag) and len(t) > len(flag) and t[len(flag)] != "/":
                rest = t[len(flag) :]
                if rest.startswith("../") and _PATHLIKE.match(rest):
                    return flag + canon_str(self.roots, self.absp(rest, cwd))
        if not t or t.startswith("-") or "=" in t or not _PATHLIKE.match(t):
            return canon_str(self.roots, t)
        if os.path.isabs(t):
            return canon_str(self.roots, os.path.normpath(t))
        a = self.absp(t, cwd)
        # A relative path that climbs out of the working directory names a file
        # outside it (buildlog writes sources relative to its build directory;
        # CMake writes them absolute): resolve it.
        if (
            a in self.known
            or t.startswith("../")
            or (cwd != self.build and ("/" in t or _EXT.search(t)))
        ):
            return canon_str(self.roots, a)
        return t

    def cstep(self, s: dict) -> dict:
        cwd = s["cwd"]
        out = {"argv": [self.ctoken(t, cwd) for t in s["argv"]], "cwd": self.cpath(cwd)}
        for k in ("stdin", "stdout", "stdout_append", "stderr"):
            if k in s:
                out[k] = s[k] if s[k] == "&1" else self.cpath(s[k], cwd)
        if "env" in s:
            out["env"] = {k: canon_str(self.roots, v) for k, v in s["env"].items()}
        if "wrapper" in s:
            out["wrapper"] = [canon_str(self.roots, t) for t in s["wrapper"]]
        if s.get("unparsed"):
            out["unparsed"] = True
        return out


def target_of(
    e: Edge, norm: Normalizer, fa: dict[str, FTarget], maps: dict
) -> tuple[str | None, str]:
    objdir = e.bindings.get("OBJECT_DIR")
    if objdir:
        t = maps["objdir"].get(norm.absp(objdir))
        if t:
            return t, "fileapi-objdir"
    outs = [norm.absp(p) for p in e.outputs + e.implicit_outputs]
    for o in outs:
        if o in maps["artifact"]:
            return maps["artifact"][o], "fileapi-artifact"
    cands = set()
    for o in outs:
        cands |= maps["source"].get(o, set())
    if cands:
        if e.section_target in cands:
            return e.section_target, "fileapi-source"
        return min(cands), "fileapi-source"
    for o in outs:
        t = maps["utility"].get(o)
        if t:
            return t, "fileapi-utility"
        b = os.path.basename(o)
        if b.startswith("cmake_object_order_depends_target_"):
            n = b[len("cmake_object_order_depends_target_") :]
            if n in fa:
                return n, "fileapi-order"
    return None, ""


def normalize(
    nf: NinjaFile,
    build: str,
    roots: Roots,
    fa: dict[str, FTarget] | None,
    scope: dict,
    variant: str | None,
    target_map: dict[str, str] | None = None,
    target_dirs: dict[str, str] | None = None,
    target_closure: set[str] | None = None,
) -> list[dict]:
    norm = Normalizer(nf, build, roots)
    fa = fa or {}
    maps = {"objdir": {}, "artifact": {}, "source": defaultdict(set), "utility": {}}
    for t in fa.values():
        maps["objdir"][t.objdir] = t.name
        for a in t.artifacts:
            maps["artifact"][a] = t.name
        for s in t.generated:
            maps["source"][s].add(t.name)
        bd = norm.absp(t.build_dir)
        aliases = [os.path.join(bd, t.name), os.path.join(bd, "CMakeFiles", t.name)]
        # CMake's top-level "Target aliases" phonies: <build>/<name>, <build>/<file name>.
        aliases += [os.path.join(norm.build, t.name)]
        aliases += [os.path.join(norm.build, os.path.basename(a)) for a in t.artifacts]
        for p in aliases:
            maps["utility"].setdefault(os.path.normpath(p), t.name)
    if target_closure is not None:
        closure = target_closure
    else:
        closure = sample_closure(scope, fa) if fa else set()
    rules = scope.get("rule", [])

    link_outputs: set[str] = set()
    pre = []
    for e in nf.edges:
        command = edge_var(nf, e, "command") if e.rule != "phony" else ""
        steps = decompose(command, norm.build) if command else []
        main = main_step_index(steps)
        generator = edge_var(nf, e, "generator") not in ("", "0") if e.rule != "phony" else False
        kind, link_type = classify(e.rule, steps, main, generator)
        if kind in ("link", "symlink"):
            link_outputs |= {norm.absp(p) for p in e.outputs + e.implicit_outputs}
        pre.append((e, command, steps, main, generator, kind, link_type))

    # First pass: direct File API attribution.
    attributed: dict[int, tuple[str | None, str]] = {}
    for i, (e, *_rest) in enumerate(pre):
        t, via = target_of(e, norm, fa, maps)
        if t is None and target_map:
            for o in e.outputs:
                t = target_map.get(norm.cpath(o))
                if t:
                    via = "target-map"
                    break
        attributed[i] = (t, via)
    # Second pass: unattributed edges inherit the unique target of their consumers,
    # then the unique target of their explicit inputs (symlinks), then the section.
    producer_of: dict[str, int] = {}
    consumers: dict[str, set[int]] = defaultdict(set)
    for i, (e, *_rest) in enumerate(pre):
        for p in e.outputs + e.implicit_outputs:
            producer_of[norm.absp(p)] = i
        for p in e.explicit + e.implicit:
            consumers[norm.absp(p)].add(i)
    for _ in range(4):
        changed = False
        for i, (e, *_rest) in enumerate(pre):
            if attributed[i][0] is not None or e.rule == "phony":
                continue
            cons = set()
            for p in e.outputs + e.implicit_outputs:
                for j in consumers.get(norm.absp(p), ()):
                    if attributed[j][0] is not None and pre[j][0].rule != "phony":
                        cons.add(attributed[j][0])
            if len(cons) == 1:
                attributed[i] = (cons.pop(), "consumer")
                changed = True
                continue
            ins = {
                attributed[producer_of[norm.absp(p)]][0]
                for p in e.explicit
                if norm.absp(p) in producer_of
            } - {None}
            if len(ins) == 1:
                attributed[i] = (ins.pop(), "input")
                changed = True
        if not changed:
            break

    records = []
    for i, (e, command, steps, main, generator, kind, link_type) in enumerate(pre):
        t, via = attributed[i]
        if t is None and e.section_target:
            t, via = e.section_target, "ninja-section"
        csteps = [norm.cstep(s) for s in steps]
        outs = list(dict.fromkeys(norm.cpath(p) for p in e.outputs))
        rec: dict = {
            "id": outs[0] if outs else f"<edge@{e.line}>",
            "outputs": outs,
            "rule": re.sub(r"__.*$", "", e.rule),
            "kind": kind,
            "phony": e.rule == "phony",
            "origin": "phony"
            if e.rule == "phony"
            else ("custom_command" if e.rule == "CUSTOM_COMMAND" else "rule"),
            "inputs": {
                "explicit": [norm.cpath(p) for p in e.explicit],
                "implicit": sorted({norm.cpath(p) for p in e.implicit}),
                "order_only": sorted({norm.cpath(p) for p in e.order_only}),
            },
        }
        iouts = sorted({norm.cpath(p) for p in e.implicit_outputs} - set(outs))
        if iouts:
            rec["implicit_outputs"] = iouts
        if link_type:
            rec["link_type"] = link_type
        if command:
            try:
                full = shlex.split(command)
            except ValueError:
                full = command.split()
            simple = (
                len(steps) == 1
                and set(steps[0]) == {"argv", "cwd"}
                and steps[0]["cwd"] == norm.build
                and steps[0]["argv"] == full
            )
            if not simple:  # the original command, when it is more than its one step
                rec["argv"] = [norm.ctoken(x, norm.build) for x in full]
            rec["steps"] = csteps
            if main is not None:
                rec["main"] = main
                prog = steps[main]["argv"][0]
                rec["tool"] = norm.ctoken(prog, steps[main]["cwd"])
                rec["tool_name"] = os.path.basename(prog)
                prog_abs = norm.absp(prog, steps[main]["cwd"])
                rec["tool_built"] = prog_abs in link_outputs
                built = []
                for s in steps:
                    for tok in s["argv"]:
                        if "=" in tok or tok.startswith("-"):
                            continue
                        for x in tok.split(","):  # e.g. gengen -p a.so,b.so
                            a = norm.absp(x, s["cwd"]) if x else ""
                            if a in link_outputs:
                                built.append(canon_str(roots, a))
                if built:
                    rec["runs_built"] = sorted(set(built))
        dep = edge_var(nf, e, "depfile") if e.rule != "phony" else ""
        if dep:
            rec["depfile"] = norm.cpath(dep)
            rec["deps"] = edge_var(nf, e, "deps")
        if e.rule != "phony":
            if edge_var(nf, e, "restat") not in ("", "0"):
                rec["restat"] = True
            if generator:
                rec["generator"] = True
            pool = edge_var(nf, e, "pool")
            if pool:
                rec["pool"] = pool
        if t is not None:
            rec["target"] = t
            rec["target_via"] = via
            if t in fa:
                rec["target_type"] = fa[t].type
                rec["dir"] = fa[t].dir
            elif target_dirs and t in target_dirs:
                rec["dir"] = target_dirs[t]
        if "dir" not in rec:
            rec["dir"] = _dir_from_output(e.outputs[0] if e.outputs else "", norm, t)
        phase, excluded = apply_rules(rec, rules, closure)
        rec["phase"] = phase
        if excluded:
            rec["excluded"] = excluded
            # Excluded records keep identity and classification only; this keeps the
            # checked-in baselines small. Use --full to keep everything.
        if variant:
            rec["variant"] = variant
        records.append(rec)
    records.sort(key=lambda r: (r["id"], r["outputs"]))
    return records


_KEEP_WHEN_EXCLUDED = ("id", "outputs", "kind", "target", "dir", "phase", "excluded", "tool_name")


def slim(rec: dict) -> dict:
    if "excluded" not in rec:
        return rec
    return {k: v for k, v in rec.items() if k in _KEEP_WHEN_EXCLUDED}


def _dir_from_output(out: str, norm: Normalizer, target: str | None) -> str:
    """CMake directory of a record without a File API target (install, package, ...)."""
    rel = os.path.relpath(norm.absp(out), norm.build)
    if rel.startswith(".."):
        return "."
    for suffix in (f"CMakeFiles/{target}.util", target):
        if target and (rel == suffix or rel.endswith("/" + suffix)):
            return rel[: -len(suffix)].strip("/") or "."
    parts = rel.split("/")
    if "CMakeFiles" in parts:
        parts = parts[: parts.index("CMakeFiles")]
    else:
        parts = parts[:-1]
    return "/".join(parts) or "."


def default_roots(src: str, build: str, llvm: str | None, extra: dict | None = None) -> Roots:
    sdk = []
    try:
        sdk.append(
            subprocess.run(
                ["xcrun", "--show-sdk-path"], capture_output=True, text=True, check=True
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        pass
    roots = {
        "$BUILD": [build],
        "$SRC": [src],
        "$LLVM": [llvm] if llvm else [],
        "$SDK": sdk,
        "$HOMEBREW": ["/opt/homebrew", "/usr/local/Homebrew"],
        "$HOME": [os.path.expanduser("~")],
        "$REPO": [
            os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        ],  # e.g. the cross file in the repo
    }
    for k, v in (extra or {}).items():
        roots.setdefault(k, []).extend(v)
    return Roots(roots)


def write_jsonl(records: list[dict], path: str) -> None:
    with open(path, "w") as f:
        f.writelines(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records)


def summarize(records: list[dict]) -> str:
    c: dict = defaultdict(lambda: defaultdict(int))
    ex: dict = defaultdict(int)
    for r in records:
        if r.get("excluded"):
            ex[r["excluded"]] += 1
        else:
            c[r["phase"]][r["kind"]] += 1
    lines = []
    for p in sorted(c):
        lines.append(f"phase {p}: " + ", ".join(f"{k}={v}" for k, v in sorted(c[p].items())))
    for reason, n in sorted(ex.items(), key=lambda t: -t[1]):
        lines.append(f"excluded {n:6d}  {reason}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Install manifest


_MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xce\xfa\xed\xfe"}


def macho_facts(path: str, roots: Roots) -> dict:
    """Architectures, install name, linked libraries and rpaths of a Mach-O file."""
    try:
        with open(path, "rb") as f:
            if f.read(4) not in _MACHO_MAGIC:
                return {}
        run = lambda *a: subprocess.run(a, capture_output=True, text=True, check=True).stdout
        archs = run("lipo", "-archs", path).split()
        lines = run("otool", "-l", path).splitlines()
    except (OSError, subprocess.CalledProcessError):
        return {}
    facts: dict = {"archs": archs}
    cmd = None
    for line in lines:
        t = line.split()
        if t[:1] == ["cmd"]:
            cmd = t[1]
        elif t[:1] in (["name"], ["path"]) and cmd in (
            "LC_ID_DYLIB",
            "LC_LOAD_DYLIB",
            "LC_RPATH",
            "LC_LOAD_WEAK_DYLIB",
        ):
            val = canon_str(roots, t[1])
            if cmd == "LC_ID_DYLIB":
                facts["install_name"] = val
            elif cmd == "LC_RPATH":
                facts.setdefault("rpaths", []).append(val)
            else:
                facts.setdefault("links", []).append(val)
    return facts


_INSTALL_PY = re.compile(r"(^|/)(python3|Halide_Python)(/|$)|PyStubs")


def install_manifest(prefix: str, roots: Roots) -> list[dict]:
    out = []
    prefix = os.path.normpath(prefix)
    for dirpath, dirnames, filenames in os.walk(prefix):
        dirnames.sort()
        for name in sorted(filenames) + [
            d for d in dirnames if os.path.islink(os.path.join(dirpath, d))
        ]:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, prefix)
            rec = {"path": "$PREFIX/" + rel}
            if os.path.islink(p):
                rec["type"] = "symlink"
                rec["link"] = canon_str(roots, os.readlink(p))
            else:
                rec["type"] = "file"
                rec["executable"] = os.access(p, os.X_OK)
                if rel.endswith(".cmake"):
                    with open(p, errors="replace") as f:
                        text = f.read()
                    leaked = sorted(
                        {m for m in re.findall(r"/(?:Users|opt|Applications)/[^\s\"';)]*", text)}
                    )
                    if leaked:
                        rec["mentions"] = sorted({canon_str(roots, m) for m in leaked})
                else:
                    rec.update(macho_facts(p, roots))
            if _INSTALL_PY.search(rel):
                rec["excluded"] = "python bindings (out of scope)"
            out.append(rec)
    out.sort(key=lambda r: r["path"])
    return out


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("normalize")
    n.add_argument("--build-ninja", required=True)
    n.add_argument("--build", required=True, help="the build directory the graph was made for")
    n.add_argument("--src", required=True)
    n.add_argument("--fileapi", help="CMake File API reply directory")
    n.add_argument("--llvm")
    n.add_argument("--scope")
    n.add_argument("--variant")
    n.add_argument("--target-map", help="JSON {normalized output: target} for non-CMake graphs")
    n.add_argument(
        "--target-dirs", help="JSON {target: source dir} for non-CMake graphs (File API's dir)"
    )
    n.add_argument(
        "--target-closure",
        help="JSON [target] for non-CMake graphs: the sampled tests' dependency closure",
    )
    n.add_argument("--root", action="append", default=[], metavar="PH=PATH")
    n.add_argument("--full", action="store_true", help="keep all fields of excluded records")
    n.add_argument("-o", "--output", required=True)
    m = sub.add_parser("install-manifest")
    m.add_argument("--prefix", required=True)
    m.add_argument("--src", required=True)
    m.add_argument("--build", required=True)
    m.add_argument("--llvm")
    m.add_argument("-o", "--output", required=True)
    t = sub.add_parser("test-regex")
    t.add_argument("--scope", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "test-regex":
        names = sampled_tests(load_scope(args.scope))
        print("^(" + "|".join(re.escape(x) for x in names) + ")$")
        return 0

    extra: dict = defaultdict(list)
    for r in getattr(args, "root", []):
        k, _, v = r.partition("=")
        extra[k].append(v)
    roots = default_roots(os.path.abspath(args.src), os.path.abspath(args.build), args.llvm, extra)
    if args.cmd == "install-manifest":
        write_jsonl(install_manifest(args.prefix, roots), args.output)
        return 0

    nf = parse_ninja(args.build_ninja)
    fa = (
        load_fileapi(args.fileapi, os.path.abspath(args.src), os.path.abspath(args.build))
        if args.fileapi
        else None
    )
    tm = None
    if args.target_map:
        with open(args.target_map) as f:
            tm = json.load(f)
    td = None
    if args.target_dirs:
        with open(args.target_dirs) as f:
            td = json.load(f)
    tc = None
    if args.target_closure:
        with open(args.target_closure) as f:
            tc = set(json.load(f))
    recs = normalize(nf, args.build, roots, fa, load_scope(args.scope), args.variant, tm, td, tc)
    if not args.full:
        recs = [slim(r) for r in recs]
    write_jsonl(recs, args.output)
    print(summarize(recs), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
