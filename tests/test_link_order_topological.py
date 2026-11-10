# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A link line lists every static archive before the archives it depends on,
transitively, as GNU ld needs: it scans each archive once, left to right, and
only pulls members for symbols already undefined. ld64 resolves across all
archives, so a wrong order is invisible on macOS; the property is therefore
asserted on the emitted build.ninja link line (any platform), and on Linux the
project is also built with GNU ld and run."""

from __future__ import annotations

import re
import shlex
import subprocess

from tests import host
from tests.test_cpp_mechanisms import cmd_of, configure, needs_souffle, project

REL = "arm64-macos.release.none"  # linux/port.py renames it to the host's

# Names sort against the dependency direction, so lexical order cannot pass by luck.
EDGES = [
    # a chain: top needs mid needs low
    ("app", "z_top"),
    ("z_top", "m_mid"),
    ("m_mid", "a_low"),
    # a diamond: both sides need the base, which sorts first
    ("app", "q_right"),
    ("app", "p_left"),
    ("q_right", "b_base"),
    ("p_left", "b_base"),
    # the runtime shape: add_halide_library's archive depends publicly on its own
    # runtime archive; two libraries, each with a runtime, and one sibling that
    # sorts between them and needs neither
    ("app", "y_gen"),
    ("y_gen", "y_gen.runtime"),
    ("app", "c_gen"),
    ("c_gen", "c_gen.runtime"),
    ("app", "d_plain"),
    # a library that needs the runtime of another one directly
    ("x_user", "c_gen"),
    ("app", "x_user"),
]
NAMES = sorted({n for e in EDGES for n in e})
LIBS = [n for n in NAMES if n != "app"]


def ident(n: str) -> str:
    return n.replace(".", "_")


def sources() -> dict[str, str]:
    files = {}
    for n in LIBS:
        callees = [ident(b) for a, b in EDGES if a == n]
        decls = "".join(f"int {c}(void);\n" for c in callees)
        calls = "".join(f" + {c}()" for c in callees)
        files[f"{n}.c"] = f"{decls}int {ident(n)}(void) {{ return 1{calls}; }}\n"
    roots = [ident(b) for a, b in EDGES if a == "app"]
    files["app.c"] = (
        "".join(f"int {r}(void);\n" for r in roots)
        + "int main(void) { return ("
        + " + ".join(f"{r}()" for r in roots)
        + ") > 0 ? 0 : 1; }\n"
    )
    return files


def spec() -> str:
    kinds = [f'["{n}", "{"executable" if n == "app" else "library"}"]' for n in NAMES]
    return f"""
target = [{", ".join(f'["{n}"]' for n in NAMES)}]
native_kind = [{", ".join(kinds)}]
default_linkage = [{", ".join(f'["{n}", "static"]' for n in LIBS)}]
c_source = [{", ".join(f'["{n}", "{n}.c"]' for n in NAMES)}]
dep = [{", ".join(f'["{a}", "{b}", "{"public" if b.endswith(".runtime") else "private"}"]' for a, b in EDGES)}]
request = [["app", "rel"]]
"""


def closure(n: str) -> set[str]:
    out: set[str] = set()
    todo = [n]
    while todo:
        for a, b in EDGES:
            if a == todo[-1] and b not in out:
                out.add(b)
                todo.append(b)
                break
        else:
            todo.pop()
    return out


def archives(cmd: str) -> list[str]:
    return [m.group(1) for w in shlex.split(cmd) if (m := re.search(r"/lib(.+)\.a$", w))]


@needs_souffle
def test_link_line_lists_every_archive_before_the_archives_it_needs(tmp_path):
    build, metrics = configure(project(tmp_path, spec(), sources()), tmp_path)
    assert metrics["errors"] == []
    line = archives(cmd_of(build, f"bin/{REL}/app"))
    assert sorted(line) == LIBS
    pos = {n: i for i, n in enumerate(line)}
    for n in LIBS:
        for dep in closure(n):
            assert pos[n] < pos[dep], f"{n} must precede {dep}: {line}"


@needs_souffle
def test_the_chained_project_links_and_runs_with_gnu_ld(tmp_path):
    if not host.LINUX:
        import pytest

        pytest.skip("Linux only: GNU ld scans archives once, left to right")
    build, metrics = configure(project(tmp_path, spec(), sources()), tmp_path)
    assert metrics["errors"] == []
    ld = subprocess.run(["ld", "--version"], capture_output=True, text=True, check=False).stdout
    assert "GNU ld" in ld
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True, capture_output=True)
    exe = build / "souffle" / "bin" / REL / "app"
    assert subprocess.run([str(exe)], check=False).returncode == 0
