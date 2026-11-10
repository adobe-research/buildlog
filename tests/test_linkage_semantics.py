# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""What a dependency edge passes on to the consumer's link line and compile lines,
for A -> B -> C with B and C static, shared or header-only (an interface target:
no kind, no sources). The expectations are CMake's:

* a static library carries its link dependencies, so a private static dependency's
  archive is on its consumer's link line (CMake's $<LINK_ONLY:...>);
* a shared library hides its private dependencies, which are linked into it;
  its public ones stay on the consumer's line;
* compile usage requirements (include directories, defines) follow public and
  interface edges only: a private dependency's requirements reach the library that
  has it and nobody else;
* a header-only library has no link input; its dependencies' edges are as written.

The same projects are built with GNU ld and run on Linux."""

from __future__ import annotations

import re
import shlex
import subprocess

import pytest

from tests import host
from tests.test_cpp_mechanisms import cmd_of, configure, needs_souffle, project

REL = "arm64-macos.release.none"  # linux/port.py renames it to the host's

# (id, B, C, A->B, B->C, link names on A's line, C's requirements reach A)
# B, C: static | shared | header (an interface target)
CASES = [
    # CMake: target_link_libraries(B PRIVATE C), B static
    ("static-private-static", "static", "static", "private", "private", ["B", "C"], False),
    ("static-public-static", "static", "static", "public", "public", ["B", "C"], True),
    ("static-private-shared", "static", "shared", "private", "private", ["B", "C"], False),
    # a shared library keeps its private dependency to itself
    ("shared-private-static", "shared", "static", "private", "private", ["B"], False),
    ("shared-public-static", "shared", "static", "public", "public", ["B", "C"], True),
    ("shared-private-shared", "shared", "shared", "private", "private", ["B"], False),
    ("shared-interface-static", "shared", "static", "private", "interface", ["B", "C"], True),
    # header-only libraries
    ("static-private-header", "static", "header", "private", "private", ["B"], False),
    ("static-public-header", "static", "header", "public", "public", ["B"], True),
    ("header-interface-static", "header", "static", "private", "interface", ["C"], True),
    ("header-interface-header", "header", "header", "private", "interface", [], True),
    ("header-public-static", "header", "static", "public", "public", ["C"], True),
]


def case_spec(b, c, ab, bc):
    kinds = [["A", "executable"]] + [
        [n, "library"] for n, k in (("B", b), ("C", c)) if k != "header"
    ]
    linkage = [[n, k] for n, k in (("B", b), ("C", c)) if k in ("static", "shared")]
    srcs = [["A", "A.c"]] + [[n, f"{n}.c"] for n, k in (("B", b), ("C", c)) if k != "header"]
    return f"""
target = [["A"], ["B"], ["C"]]
native_kind = {kinds}
default_linkage = {linkage}
c_source = {srcs}
prop = [["C", "cc.include_dir", "src:cinc", "public"], ["C", "cc.define", "FROM_C", "public"]]
dep = [["A", "B", "{ab}"], ["B", "C", "{bc}"]]
request = [["A", "rel"]]
""".replace("'", '"')


def case_sources(b, c):
    files = {"cinc/c.h": "#define C_H 1\n"}
    if c != "header":
        files["C.c"] = "int c_fn(void) { return 3; }\n"
    if b != "header":
        callee = "c_fn()" if c != "header" else "1"
        decl = "int c_fn(void);\n" if c != "header" else ""
        files["B.c"] = f"{decl}int b_fn(void) {{ return {callee} + 1; }}\n"
    if b != "header":
        files["A.c"] = (
            "int b_fn(void);\nint main(void) { return b_fn() == 4 || b_fn() == 2 ? 0 : 1; }\n"
        )
    elif c != "header":
        files["A.c"] = "int c_fn(void);\nint main(void) { return c_fn() == 3 ? 0 : 1; }\n"
    else:
        files["A.c"] = "int main(void) { return 0; }\n"
    return files


def build_case(tmp_path, b, c, ab, bc):
    files = case_sources(b, c)
    proj = project(
        tmp_path, case_spec(b, c, ab, bc), {k: v for k, v in files.items() if "/" not in k}
    )
    (proj / "src" / "cinc").mkdir()
    (proj / "src" / "cinc" / "c.h").write_text(files["cinc/c.h"])
    return configure(proj, tmp_path)


def link_names(cmd):
    out = []
    for w in shlex.split(cmd):
        if m := re.search(r"/lib([BC])\.(?:a|so|dylib)$", w):
            out.append(m.group(1))
    return out


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
@needs_souffle
def test_linkage_and_usage_propagation(tmp_path, case):
    _, b, c, ab, bc, link, reaches = case
    build, metrics = build_case(tmp_path, b, c, ab, bc)
    assert metrics["errors"] == []
    line = link_names(cmd_of(build, f"bin/{REL}/A"))
    assert sorted(line) == sorted(link), line
    if "B" in line and "C" in line:
        assert line.index("B") < line.index("C"), line  # the user precedes what it needs
    seen = cmd_of(build, f"obj/{REL}/A/A.c.o")
    assert ("src/cinc" in seen) == reaches, seen
    assert ("-DFROM_C" in seen) == reaches, seen
    if b != "header":
        # B uses C unless the edge is interface-only (passed on, not used)
        own = cmd_of(build, f"obj/{REL}/B/B.c.o")
        assert ("src/cinc" in own and "-DFROM_C" in own) == (bc != "interface")


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
@needs_souffle
def test_linkage_cases_link_and_run_with_gnu_ld(tmp_path, case):
    if not host.LINUX:
        pytest.skip("Linux only: GNU ld scans archives once, left to right")
    _, b, c, ab, bc, _, _ = case
    build, metrics = build_case(tmp_path, b, c, ab, bc)
    assert metrics["errors"] == []
    ld = subprocess.run(["ld", "--version"], capture_output=True, text=True, check=False).stdout
    assert "GNU ld" in ld
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True, capture_output=True)
    exe = build / "souffle" / "bin" / REL / "A"
    assert subprocess.run([str(exe)], check=False).returncode == 0
