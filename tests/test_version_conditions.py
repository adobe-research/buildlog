# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Version comparison in conditions: `cxx.version >= 13`,
`package.<name>.version > 1.2.3` (core L1_targets.dl, conditions.py).

A version is dotted numbers compared as numbers, a missing component is 0, a
suffix after the numbers is ignored. The attribute is discovered, so a
condition that decides what is built may not read it."""

from __future__ import annotations

import json
import re

import pytest
from buildlog_cpp.probes import tool_version

from buildlog import conditions
from buildlog.cli import main
from buildlog.engine import configure_project

from . import host
from .test_cpp_mechanisms import cmd_of, configure, needs_souffle, project
from .test_loader import _rejected

SOURCES = {"main.cpp": "int main() { return 0; }\n", "util.c": "int util(void) { return 0; }\n"}
REQUEST = 'request = [["app", "rel"]]\n'
APP = """
[target.app]
kind = "executable"
sources = ["main.cpp", "util.c"]
"""

# ---- the syntax (no Soufflé) ----------------------------------------------


def test_infix_syntax():
    parse = lambda t: conditions.parse(t, "t")
    assert parse("cxx.version >= 13") == "$Ge(cxx.version, 13)"
    assert parse("cxx.version>13.1") == "$Gt(cxx.version, 13.1)"
    assert parse("cc.version < 9") == "$Not($Ge(cc.version, 9))"
    assert (
        parse("package.zlib.version <= 1.2.3-rc1") == "$Not($Gt(package.zlib.version, 1.2.3-rc1))"
    )
    assert parse("!(cxx.version >= 13) & X") == "$And($Not($Ge(cxx.version, 13)), $Named(X))"
    assert not conditions.is_name("cxx.version>=13")
    with pytest.raises(Exception, match="unexpected end"):
        parse("cxx.version >= ")
    with pytest.raises(Exception, match="expected a version"):
        parse("cxx.version >= >")


# ---- the toolchain's version ----------------------------------------------


@pytest.mark.parametrize(
    ("banner", "version"),
    [
        ("g++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0", "13.3.0"),
        ("g++ (GCC) 14.2.0", "14.2.0"),
        ("Apple clang version 21.0.0 (clang-2100.0.0)", "21.0.0"),
        ("clang version 22.1.0git\nTarget: aarch64-linux-gnu", "22.1.0git"),
    ],
)
def test_toolchain_version_from_banner(monkeypatch, banner, version):
    monkeypatch.setattr("buildlog_cpp.probes.which", lambda argv: banner)
    assert tool_version("c++") == version


def components(version: str) -> list[int]:
    """The reference semantics: leading dotted numbers, a suffix ignored, at most 8."""
    m = re.match(r"\d+(?:\.\d+)*", version)
    assert m, version
    return [int(x) for x in m[0].split(".")][:8]


def compare(a: str, b: str) -> int:
    x, y = components(a), components(b)
    n = max(len(x), len(y))
    x, y = x + [0] * (n - len(x)), y + [0] * (n - len(y))
    return (x > y) - (x < y)


OPS = {
    ">=": lambda r: r >= 0,
    ">": lambda r: r > 0,
    "<=": lambda r: r <= 0,
    "<": lambda r: r < 0,
}


@pytest.mark.integration
@needs_souffle
def test_toolchain_version_comparisons(tmp_path):
    host_version = tool_version(host.CXX)
    major = components(host_version)[0]
    literals = [
        str(major),
        str(major - 1),
        str(major + 1),
        f"{major}.0.0",
        f"{major}.0.1",
        f"{major}.0.0.1",
        f"{major - 1}.99",
        "9",  # numeric, not lexicographic: 21 > 9
        f"{major}.0.0-rc1",
        f"{major}.x",
    ]
    rows, expect = [], {}
    for i, lit in enumerate(literals):
        for j, (op, ok) in enumerate(OPS.items()):
            name = f"V{i}_{j}"
            rows.append(f'when."cxx.version {op} {lit}".private."cc.define" = "{name}"')
            expect[name] = ok(compare(host_version, lit))
    proj = project(tmp_path, REQUEST + APP + "\n".join(rows) + "\n", SOURCES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    cmd = cmd_of(build, "app/main.cpp.o")
    got = {n: f"-D{n} " in cmd + " " for n in expect}
    assert got == expect
    assert any(expect.values()) and not all(expect.values())


@pytest.mark.integration
@needs_souffle
def test_c_and_cxx_versions_and_a_named_condition(tmp_path):
    major = components(tool_version(host.CXX))[0]
    proj = project(
        tmp_path,
        f"""
condition = [["new_enough", "$Ge(cxx.version, {major})"], ["too_new", "cc.version > {major + 1}"]]
prop_when = [["app", "cc.define", "NEW_ENOUGH", "private", "new_enough"], ["app", "cc.define", "TOO_NEW", "private", "too_new"], ["app", "cc.define", "OLD", "private", "cc.version < {major}"]]
"""
        + REQUEST
        + APP,
        SOURCES,
    )
    build, _ = configure(proj, tmp_path)
    cmd = cmd_of(build, "app/util.c.o")
    assert "-DNEW_ENOUGH" in cmd and "-DTOO_NEW" not in cmd and "-DOLD" not in cmd


def test_a_non_version_attribute_is_rejected(tmp_path, capsys):
    proj = project(
        tmp_path,
        REQUEST + APP + 'when."cxx.family >= 3".private."cc.define" = "X"\n',
        SOURCES,
    )
    with pytest.raises(BaseException) as exc:
        configure_project(proj, tmp_path / "build", emit=False, update_lock=True)
    text = str(getattr(exc.value, "errors", "")) + capsys.readouterr().err
    assert "compares a non-version attribute" in text and "cxx.family" in text


def test_a_malformed_version_is_rejected(tmp_path, capsys):
    proj = project(
        tmp_path, REQUEST + APP + 'when."cxx.version >= v13".private."cc.define" = "X"\n', SOURCES
    )
    with pytest.raises(BaseException) as exc:
        configure_project(proj, tmp_path / "build", emit=False, update_lock=True)
    text = str(getattr(exc.value, "errors", "")) + capsys.readouterr().err
    assert "not a version: it must start with a digit" in text and "v13" in text


# ---- W5: what is built may not depend on a version -------------------------


@pytest.mark.integration
@needs_souffle
@pytest.mark.parametrize("where", ["request_when", "kind_when"])
def test_a_condition_that_decides_what_is_built_may_not_compare_a_version(tmp_path, capsys, where):
    gate = {
        "request_when": 'request_when = [["extra", "cxx.version >= 1"]]\n',
        "kind_when": 'target_kind_when = [["extra", "library", "package.zlib.version > 1"]]\n',
    }[where]
    proj = project(
        tmp_path,
        REQUEST
        + gate
        + APP
        + """
[target.extra]
kind = "library"
sources = ["util.c"]
""",
        SOURCES,
    )
    with pytest.raises(BaseException) as exc:
        configure_project(proj, tmp_path / "build", emit=False, update_lock=True)
    text = str(getattr(exc.value, "errors", "")) + capsys.readouterr().err
    assert "a condition deciding what is built may test only options" in text
    assert ">=" in text or ">" in text and "version" in text


def test_a_rule_feeding_request_from_a_version_is_rejected_by_w5(tmp_path):
    """The layer check, not the per-condition error: a rule that makes
    `request` read the version attribute (which a comparison reads) reaches the
    probe result the version came from."""
    (error,) = _rejected(tmp_path, "buildlog-cpp", "request-reads-version")
    assert error.startswith("request depends on the probe result res_")
    assert (
        "request <- config_attr (tests/fixtures/rejected/share/buildlog/request-reads-version/"
        in error
    )
    assert "W5" in error


# ---- a package's version --------------------------------------------------

PC = "Name: vlib\nDescription: d\nVersion: {v}\nLibs: -lvlib\n"
# (version, {literal: [ops that hold]}): the semantics, one case per row
PACKAGE_CASES = [
    ("1.2.3", {"1.2.3": ">=,<=", "1.2": ">=,>", "1.10": "<=,<", "1.2.4": "<=,<", "1": ">=,>"}),
    ("1.10", {"1.9": ">=,>", "1.9.9": ">=,>", "1.10.0": ">=,<=", "2": "<=,<", "1.10.0.1": "<=,<"}),
    ("2", {"2.0.0": ">=,<=", "2.0.0.0": ">=,<=", "1.99.99": ">=,>", "2.0.1": "<=,<"}),
    ("3.4.5-rc1", {"3.4.5": ">=,<=", "3.4.4": ">=,>", "3.4.6": "<=,<", "3.4.5-rc2": ">=,<="}),
]


def holds(version: str, lit: str) -> set[str]:
    r = compare(version, lit)
    return {op for op, ok in OPS.items() if ok(r)}


def package_project(tmp_path, version, literals):
    pc = tmp_path / "vlib.pc"
    pc.write_text(PC.format(v=version))
    rows = []
    for i, lit in enumerate(literals):
        for j, op in enumerate(OPS):
            rows.append(f'when."package.vlib.version {op} {lit}".private."cc.define" = "P{i}_{j}"')
    spec = (
        f'package_file = [["pc", {json.dumps(str(pc))}]]\n'
        + REQUEST
        + """
[target.app]
kind = "executable"
sources = ["main.cpp"]
deps.private = ["vlib"]
"""
        + "\n".join(rows)
        + "\n"
    )
    return project(tmp_path, spec, SOURCES)


@pytest.mark.integration
@needs_souffle
@pytest.mark.parametrize(("version", "cases"), PACKAGE_CASES)
def test_package_version_comparisons(tmp_path, version, cases):
    literals = list(cases)
    proj = package_project(tmp_path, version, literals)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    cmd = cmd_of(build, "app/main.cpp.o") + " "
    got = {
        (lit, op)
        for i, lit in enumerate(literals)
        for j, op in enumerate(OPS)
        if f"-DP{i}_{j} " in cmd
    }
    want = {(lit, op) for lit in literals for op in holds(version, lit)}
    assert got == want
    # the table above states the semantics; the reference agrees with it
    for lit, ops in cases.items():
        assert set(ops.split(",")) <= holds(version, lit)


@pytest.mark.integration
@needs_souffle
def test_a_package_version_that_is_not_there_is_an_error(tmp_path, capsys):
    proj = package_project(tmp_path, "1.0", ["1"])
    spec = (proj / "spec.toml").read_text().replace("package.vlib.version", "package.nope.version")
    (proj / "spec.toml").write_text(spec)
    with pytest.raises(BaseException) as exc:
        configure_project(proj, tmp_path / "build", emit=False, update_lock=True)
    text = str(getattr(exc.value, "errors", "")) + capsys.readouterr().err
    assert "version undefined where a condition tests it" in text
    assert "package.nope.version" in text


@pytest.mark.integration
@needs_souffle
def test_why_reaches_the_version_fact(tmp_path, capsys):
    proj = package_project(tmp_path, "1.10", ["1.9"])
    build = tmp_path / "build"
    configure_project(proj, build, update_lock=True)
    capsys.readouterr()
    assert main(["why", "-C", str(build), "--flag", "-DP0_0", str(proj)]) == 0
    text = capsys.readouterr().out
    for needle in (
        "holds(",
        '$Ge("package.vlib.version", "1.9")',
        "config_attr(",
        '"package.vlib.version", "1.10.0"',
        "package_version(",
        "vlib.pc",  # the metadata file and line the version came from
    ):
        assert needle in text, (needle, text)
    # and the sort keys the comparison read
    assert "ver_key(" in text
