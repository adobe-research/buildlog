# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Install and export: the nlohmann/json case study (case-studies/nlohmann-json).

The project is installed with buildlog into a fresh prefix and compared
against CMake's install (baseline/, run-cmake.sh). One downstream spec
(downstream/buildlog) consumes nlohmann_json by every route a user can
choose: built in-tree (source_project), imported from buildlog's install
(CPS, pkg-config, CMake export) and imported from CMake's install. Imported
= built: the downstream's derived usage facts agree across routes, modulo
where the package's root is and ledgered differences (README.md).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog import export_formats
from buildlog.engine import configure_project
from buildlog.errors import SpecError
from buildlog.paths import souffle_path
from tests import host

from .test_why import spec_line

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "nlohmann-json"
PROJECT = CASE / "project"
PRESETS = PROJECT / "presets"
DOWN = CASE / "downstream" / "buildlog"
BASELINE = host.baseline_dir(CASE)
MAIN = CASE / "downstream" / "main.cpp"
SRC = CASE / "upstream"
REL = "arm64-macos.release.none"
VARIANTS = ["default", "diagnostics"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "include" / "nlohmann" / "json.hpp").exists(),
        reason="the pinned nlohmann/json checkout is absent",
    ),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]
needs_cmake = pytest.mark.skipif(shutil.which("cmake") is None, reason="cmake not installed")
needs_pkg_config = pytest.mark.skipif(
    shutil.which("pkg-config") is None, reason="pkg-config not installed"
)


def configure(project: Path, build: Path, users: list[Path], **kw) -> dict:
    _, metrics = configure_project(project, build, user_files=users, **kw)
    assert metrics["errors"] == []
    return metrics


def ninja(build: Path, *args: str, env: dict | None = None, check: bool = True):
    return subprocess.run(
        ["ninja", "-C", str(build / "souffle"), *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def install(tmp: Path, variant: str, prefix: Path) -> Path:
    """buildlog's install of nlohmann_json (the variant's options) at prefix."""
    build = tmp / f"nlohmann-{variant}"
    users = [PRESETS / "apple-clang-arm64.toml", PRESETS / "install-prefix.toml"]
    if variant == "diagnostics":
        users.append(PRESETS / "diagnostics.toml")
    configure(PROJECT, build, users, var_overrides={"INSTALL_PREFIX": str(prefix)})
    ninja(build, "install")
    return build


@pytest.fixture(scope="session")
def installs(tmp_path_factory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("nlohmann")
    out = {}
    for v in VARIANTS:
        out[v] = tmp / f"prefix-{v}"
        install(tmp, v, out[v])
    return out


@pytest.fixture(scope="session")
def cmake_installs(tmp_path_factory) -> dict[str, Path]:
    """CMake's own install of the pinned checkout (as baseline/run-cmake.sh)."""
    if shutil.which("cmake") is None:
        pytest.skip("cmake not installed")
    tmp = tmp_path_factory.mktemp("nlohmann-cmake")
    out = {}
    for v in VARIANTS:
        b, out[v] = tmp / f"build-{v}", tmp / f"prefix-{v}"
        opts = ["-DJSON_Diagnostics=ON"] if v == "diagnostics" else []
        subprocess.run(
            ["cmake", "-S", str(SRC), "-B", str(b), "-G", "Ninja", "-DJSON_BuildTests=OFF"]
            + [f"-DCMAKE_INSTALL_PREFIX={out[v]}", *opts],
            check=True,
            capture_output=True,
        )
        subprocess.run(["cmake", "--install", str(b)], check=True, capture_output=True)
    return out


def tree(prefix: Path) -> dict[str, str]:
    return {
        str(p.relative_to(prefix)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(prefix.rglob("*"))
        if p.is_file()
    }


def baseline_tree(variant: str) -> dict[str, str]:
    rows = (BASELINE / variant / "installed-tree.txt").read_text().splitlines()
    return {r.split()[0].removeprefix("./"): r.split()[2] for r in rows}


# ---- the downstream, by route ------------------------------------------------

ROUTES = ["in-tree", "cps", "pc", "cmake"]


def downstream(tmp: Path, route: str, variant: str, prefix: Path | None = None) -> Path:
    """Configure downstream/buildlog with nlohmann_json bound by route: built
    in-tree (with the variant's options as this build's user facts) or
    imported from the install at prefix."""
    name = f"down-{route}-{variant}-{prefix.parent.name if prefix else 'src'}"
    build = tmp / name
    users = [DOWN / "presets" / "apple-clang-arm64.toml"]
    kw = {}
    if route == "in-tree":
        users.append(DOWN / "presets" / "in-tree.toml")
        if variant == "diagnostics":
            users.append(DOWN / "presets" / "nlohmann-diagnostics.toml")
    else:
        users.append(DOWN / "presets" / f"import-{route}.toml")
        kw["var_overrides"] = {"NLOHMANN_PREFIX": str(prefix)}
    configure(DOWN, build, users, **kw)
    return build


def usage(build: Path, root: Path) -> set[tuple[str, ...]]:
    """The downstream program's derived usage facts (use and use_join rows for
    app), with the package root (source tree or prefix) as $ROOT."""
    root = os.path.realpath(root)
    out = set()
    for rel in ("use", "use_join"):
        with open(build / "souffle" / "facts" / f"{rel}.csv") as f:
            for row in csv.reader(f, delimiter="\t"):
                if row[0] != "app":
                    continue
                vals = []
                for v in row[2:]:
                    if v.startswith("/"):
                        v = os.path.normpath(os.path.realpath(v))
                        if v == root or v.startswith(root + "/"):
                            v = "$ROOT" + v[len(root) :]
                    vals.append(v)
                out.add((rel, *vals))
    return out


def in_tree_usage(tmp: Path, variant: str) -> set[tuple[str, ...]]:
    return usage(downstream(tmp, "in-tree", variant), SRC)


EXPECTED = {
    "default": {
        ("use", "cc.include_dir", "$ROOT/include", "1"),
        ("use_join", "cxx.std", "11"),
    },
}
EXPECTED["diagnostics"] = EXPECTED["default"] | {("use", "cc.define", "JSON_DIAGNOSTICS=1", "1")}


def run_app(build: Path) -> str:
    ninja(build)
    exe = build / "souffle" / "bin" / REL / "app"
    return subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout


# ---- buildlog's install against CMake's -----------------------------------------


def test_installed_tree_matches_cmake(installs):
    for v in VARIANTS:
        ours, theirs = tree(installs[v]), baseline_tree(v)
        # CPS and the buildlog package are buildlog's own exports; CMake 3.x writes neither.
        assert set(ours) - set(theirs) == {
            "share/cps/nlohmann_json.cps",
            "share/buildlog/nlohmann_json/manifest.toml",
            "share/buildlog/nlohmann_json/nlohmann_json.facts.dl",
        }
        assert set(theirs) <= set(ours)
        headers = [f for f in theirs if f.startswith("include/")]
        assert len(headers) == 46
        assert all(ours[f] == theirs[f] for f in headers)
        assert (
            ours["include/nlohmann/json.hpp"]
            == hashlib.sha256((SRC / "include" / "nlohmann" / "json.hpp").read_bytes()).hexdigest()
        )


def test_exported_files(installs):
    cps = (installs["default"] / "share" / "cps" / "nlohmann_json.cps").read_text()
    doc = json.loads(cps)
    assert doc["version"] == "3.12.0" and doc["compat_version"] == "3.0.0"
    assert doc["cps_path"] == "@prefix@/share/cps"
    comp = doc["components"]["nlohmann_json"]
    assert comp == {
        "type": "interface",
        "compile_features": ["c++11"],
        "includes": ["@prefix@/include"],
    }
    diag = json.loads((installs["diagnostics"] / "share" / "cps" / "nlohmann_json.cps").read_text())
    assert diag["components"]["nlohmann_json"]["definitions"] == {"*": {"JSON_DIAGNOSTICS": "1"}}
    for v, cflags in [
        ("default", "-I${prefix}/include"),
        ("diagnostics", "-DJSON_DIAGNOSTICS=1 -I${prefix}/include"),
    ]:
        pc = (installs[v] / "share" / "pkgconfig" / "nlohmann_json.pc").read_text()
        assert pc.startswith("prefix=${pcfiledir}/../..\n")
        assert f"Cflags: {cflags}\n" in pc and "Version: 3.12.0\n" in pc
    targets = (
        installs["diagnostics"] / "share" / "cmake" / "nlohmann_json" / "nlohmann_jsonTargets.cmake"
    )
    t = targets.read_text()
    assert 'INTERFACE_COMPILE_DEFINITIONS "JSON_DIAGNOSTICS=1"' in t
    assert 'INTERFACE_INCLUDE_DIRECTORIES "${_IMPORT_PREFIX}/include"' in t
    assert "add_library(nlohmann_json::nlohmann_json INTERFACE IMPORTED)" in t
    # No absolute path of the install survives in any exported file.
    for v in VARIANTS:
        for f in (installs[v] / "share").rglob("*"):
            if f.is_file():
                assert str(installs[v]) not in f.read_text(), f


def test_no_prefix_is_an_error(tmp_path):
    build = tmp_path / "b"
    configure(PROJECT, build, [PRESETS / "apple-clang-arm64.toml"])
    # The build itself (the export files) needs no prefix ...
    ninja(build)
    assert (
        build / "souffle" / "export" / REL / "nlohmann_json" / "cpsdir" / "nlohmann_json.cps"
    ).exists()
    # ... installing does, and there is no default.
    r = ninja(build, "install", check=False)
    assert r.returncode != 0
    assert "no install prefix" in r.stdout and "install_prefix" in r.stdout


def test_relative_prefix_is_an_error(tmp_path):
    user = tmp_path / "prefix.toml"
    user.write_text('install_prefix = "relative/usr"\n')
    with pytest.raises(SystemExit):
        configure_project(
            PROJECT, tmp_path / "b", user_files=[PRESETS / "apple-clang-arm64.toml", user]
        )
    assert "install prefix is not an absolute path" in (tmp_path / "b" / "metrics.json").read_text()


def test_destdir_stages_under_the_prefix(tmp_path):
    prefix, stage = tmp_path / "usr", tmp_path / "stage"
    build = tmp_path / "b"
    configure(
        PROJECT,
        build,
        [PRESETS / "apple-clang-arm64.toml", PRESETS / "install-prefix.toml"],
        var_overrides={"INSTALL_PREFIX": str(prefix)},
    )
    ninja(build, "install", env={**os.environ, "DESTDIR": str(stage)})
    assert not prefix.exists()
    staged = stage / str(prefix).lstrip("/")
    assert len(tree(staged)) == 53
    assert (staged / "include" / "nlohmann" / "json.hpp").exists()


def test_user_overrides_layout(tmp_path):
    user = tmp_path / "layout.toml"
    user.write_text(
        'install_dir = [["includedir", "include/v3"], ["pkgconfigdir", "lib/pkgconfig"]]\n'
    )
    build = tmp_path / "b"
    prefix = tmp_path / "usr"
    configure(
        PROJECT,
        build,
        [PRESETS / "apple-clang-arm64.toml", PRESETS / "install-prefix.toml", user],
        var_overrides={"INSTALL_PREFIX": str(prefix)},
    )
    ninja(build, "install")
    assert (prefix / "include" / "v3" / "nlohmann" / "json.hpp").exists()
    pc = (prefix / "lib" / "pkgconfig" / "nlohmann_json.pc").read_text()
    assert "Cflags: -I${prefix}/include/v3\n" in pc
    cps = (prefix / "share" / "cps" / "nlohmann_json.cps").read_text()
    assert '"@prefix@/include/v3"' in cps


# ---- imported = built ------------------------------------------------------------


@pytest.mark.parametrize("variant", VARIANTS)
def test_imported_equals_built(tmp_path, installs, variant):
    """The headline check: the downstream derives the same usage facts whether
    nlohmann_json is built in-tree or imported from buildlog's install by CPS
    or by CMake's export file. pkg-config cannot say cxx.std (README ledger)."""
    built = in_tree_usage(tmp_path, variant)
    assert built == EXPECTED[variant]
    for route in ("cps", "cmake"):
        assert (
            usage(downstream(tmp_path, route, variant, installs[variant]), installs[variant])
            == built
        ), route
    pc = usage(downstream(tmp_path, "pc", variant, installs[variant]), installs[variant])
    assert pc == {f for f in built if f[1] != "cxx.std"}


@needs_cmake
@pytest.mark.parametrize("variant", VARIANTS)
def test_cmake_install_imported_equals_built(tmp_path, cmake_installs, variant):
    """Importing CMake's install through its export file derives the same
    facts too. Its .pc drops the define (upstream writes no Cflags for it)."""
    built = in_tree_usage(tmp_path, variant)
    prefix = cmake_installs[variant]
    assert usage(downstream(tmp_path, "cmake", variant, prefix), prefix) == built
    pc = usage(downstream(tmp_path, "pc", variant, prefix), prefix)
    assert pc == {f for f in built if f[1] not in ("cxx.std", "cc.define")}


@pytest.mark.parametrize("route", ROUTES)
def test_downstream_builds_and_runs(tmp_path, installs, route):
    for v in VARIANTS:
        build = downstream(tmp_path, route, v, None if route == "in-tree" else installs[v])
        out = run_app(build)
        assert "version 3.12.0" in out
        assert f"JSON_DIAGNOSTICS={1 if v == 'diagnostics' else 0}" in out


def test_relocatable(tmp_path, installs):
    moved = tmp_path / "elsewhere" / "prefix"
    shutil.copytree(installs["diagnostics"], moved)
    for route in ("cps", "pc", "cmake"):
        build = downstream(tmp_path, route, "diagnostics", moved)
        facts = usage(build, moved)
        assert ("use", "cc.include_dir", "$ROOT/include", "1") in facts, route
        assert ("use", "cc.define", "JSON_DIAGNOSTICS=1", "1") in facts, route


# ---- foreign consumers of buildlog's install ----------------------------------


def _cmake_consumer(
    tmp: Path, prefix: Path, lists: Path | None = None
) -> subprocess.CompletedProcess:
    src = lists or (CASE / "downstream" / "cmake")
    b = tmp / "cmake-consumer"
    return subprocess.run(
        ["cmake", "-S", str(src), "-B", str(b), "-G", "Ninja", f"-DCMAKE_PREFIX_PATH={prefix}"],
        capture_output=True,
        check=False,
        text=True,
    )


@needs_cmake
@pytest.mark.parametrize("variant", VARIANTS)
def test_cmake_find_package_consumer(tmp_path, installs, variant):
    r = _cmake_consumer(tmp_path, installs[variant])
    assert r.returncode == 0, r.stdout + r.stderr
    b = tmp_path / "cmake-consumer"
    subprocess.run(["cmake", "--build", str(b)], check=True, capture_output=True)
    out = subprocess.run([str(b / "app")], capture_output=True, text=True, check=True).stdout
    assert "version 3.12.0" in out
    assert f"JSON_DIAGNOSTICS={1 if variant == 'diagnostics' else 0}" in out


@needs_cmake
def test_cmake_version_compatibility(tmp_path, installs):
    """SameMajorVersion: 3.x is found, 4 and 3.13 are not."""
    for req, ok in [
        ("3", True),
        ("3.11", True),
        ("3.12.0", True),
        ("3.13", False),
        ("4", False),
        ("2", False),
    ]:
        d = tmp_path / f"v{req}"
        d.mkdir()
        (d / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\nproject(v NONE)\n"
            f"find_package(nlohmann_json {req} CONFIG REQUIRED)\n"
        )
        r = _cmake_consumer(d, installs["default"], d)
        assert (r.returncode == 0) == ok, (req, r.stderr)


@needs_pkg_config
@pytest.mark.parametrize("variant", VARIANTS)
def test_pkg_config_consumer(tmp_path, installs, variant):
    env = {**os.environ, "PKG_CONFIG_PATH": str(installs[variant] / "share" / "pkgconfig")}
    flags = subprocess.run(
        ["pkg-config", "--cflags", "nlohmann_json"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.split()
    assert ("-DJSON_DIAGNOSTICS=1" in flags) == (variant == "diagnostics")
    exe = tmp_path / "app"
    subprocess.run(["c++", "-std=c++11", *flags, str(MAIN), "-o", str(exe)], check=True)
    out = subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout
    assert f"JSON_DIAGNOSTICS={1 if variant == 'diagnostics' else 0}" in out


# ---- provenance ------------------------------------------------------------------


def _why(build: Path, flag: str, output: str) -> str:
    return subprocess.run(
        ["uv", "run", "--frozen", "buildlog", "why", "-C", str(build), "--leaves"]
        + ["--flag", flag, "--output", output, str(PROJECT)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_why_installed_file(tmp_path):
    prefix = tmp_path / "usr"
    build = tmp_path / "b"
    configure(
        PROJECT,
        build,
        [PRESETS / "apple-clang-arm64.toml", PRESETS / "install-prefix.toml"],
        var_overrides={"INSTALL_PREFIX": str(prefix)},
    )
    out = _why(build, str(prefix / "include" / "nlohmann" / "json.hpp"), "install")
    # The layout default, the user's prefix and the author's mapping and file.
    assert 'install_dir_default("includedir", "include")  ← plugins/buildlog-cpp' in out
    assert (
        f'install_prefix("{prefix}")  ← case-studies/nlohmann-json/project/presets/install-prefix.toml'
        in out
    )
    assert (
        'install_map("nlohmann_json", "src:include", "includedir", "")  ← case-studies/nlohmann-json/project/buildlog.toml'
        in out
    )
    line = spec_line(PROJECT / "headers.toml", "[target.nlohmann_json.install_when]")
    assert (
        'target_install_when("nlohmann_json", "JSON_MultipleHeaders", "src:include/nlohmann/json.hpp")'
        f"  ← case-studies/nlohmann-json/project/headers.toml:{line}" in out
    )
    cps = f"export/{REL}/nlohmann_json/cpsdir/nlohmann_json.cps"
    out = _why(build, "--value=nlohmann_json:compile_features=c++11", cps)
    assert (
        'target_interface("nlohmann_json", "cxx.std", "11")  ← case-studies/nlohmann-json/project/buildlog.toml'
        in out
    )
    assert 'export_spelling("cps", "cxx.std", "compile_features")  ← plugins/buildlog-cpp' in out


def test_why_exported_define(tmp_path):
    build = tmp_path / "b"
    configure(PROJECT, build, [PRESETS / "apple-clang-arm64.toml", PRESETS / "diagnostics.toml"])
    cps = f"export/{REL}/nlohmann_json/cpsdir/nlohmann_json.cps"
    out = _why(build, "--value=nlohmann_json:definitions=JSON_DIAGNOSTICS=1", cps)
    assert (
        'option_value("JSON_Diagnostics", "on")  ← case-studies/nlohmann-json/project/presets/diagnostics.toml'
        in out
    )
    assert 'option("JSON_Diagnostics")' in out
    assert (
        'target_when("nlohmann_json", "JSON_Diagnostics", "interface", "cc.define", "JSON_DIAGNOSTICS=1")'
        in out
    )


# ---- the writers ------------------------------------------------------------------


def _pkg(*extra: str) -> list[str]:
    return [
        "--format", "cps", "-o", "x", "--package", "p", "--install-path", "share/cps/p.cps",
        "--version", "1.2.3", "--component=interface:p", *extra,
    ]  # fmt: skip


def test_writer_compat_versions():
    for compat, want in [
        ("same_major", "1.0.0"),
        ("same_minor", "1.2.0"),
        ("exact", None),
        ("any_newer", "0.0.0"),
    ]:
        p = export_formats.parse(_pkg("--compat", compat))
        assert export_formats._compat_version(p) == want


def test_writer_cmake_version_needs_arch_independence():
    argv = _pkg("--compat", "same_major")
    argv[1] = "cmake-version"
    with pytest.raises(export_formats.ExportError):
        export_formats.write_cmake_version(export_formats.parse(argv))
    ok = export_formats.write_cmake_version(export_formats.parse([*argv, "--arch-independent"]))
    assert 'PACKAGE_FIND_VERSION_MAJOR STREQUAL "1"' in ok and "CMAKE_SIZEOF_VOID_P" not in ok


def test_writer_relative_prefix_depth():
    p = export_formats.parse(_pkg("--compat", "same_major"))
    assert p.up == 2
    t = export_formats.write_cmake_targets(
        export_formats.parse(
            ["--format", "cmake-targets", "-o", "x", "--package", "p"]
            + ["--install-path", "lib/cmake/p/pTargets.cmake", "--component=interface:p"]
            + [
                "--path=p:INTERFACE_INCLUDE_DIRECTORIES=include",
                "--value=p:INTERFACE_COMPILE_DEFINITIONS=A",
            ]
        )
    )
    assert t.count('get_filename_component(_IMPORT_PREFIX "${_IMPORT_PREFIX}" PATH)') == 3
    assert 'INTERFACE_COMPILE_DEFINITIONS "A"' in t


# ---- a package of two components (tests/fixtures/export-components) ------------

PKGX = ROOT / "tests" / "fixtures" / "export-components"
CONFLICT = ROOT / "tests" / "fixtures" / "export-conflict"


def test_components_and_requires_round_trip(tmp_path):
    """pkgx offers a and b, a depends on b: the export states the edge (CPS
    requires, CMake INTERFACE_LINK_LIBRARIES), and a downstream of pkgx:a
    derives b's usage too, the same by every route. The downstream names
    pkgx:a and pkgx (its default components) the same way by every route,
    with no alias (package names)."""
    prefix = tmp_path / "usr"
    user = tmp_path / "prefix.toml"
    user.write_text(f'install_prefix = "{prefix}"\n')
    build = tmp_path / "pkgx"
    configure(PKGX / "project", build, [PRESETS / "apple-clang-arm64.toml", user])
    ninja(build, "install")
    cps = json.loads((prefix / "share" / "cps" / "pkgx.cps").read_text())
    assert cps["compat_version"] == "1.2.0"
    assert cps["components"]["a"]["requires"] == [":b"]
    assert cps["components"]["b"]["includes"] == ["@prefix@/include/pkgx"]
    targets = (prefix / "share" / "cmake" / "pkgx" / "pkgxTargets.cmake").read_text()
    assert 'INTERFACE_LINK_LIBRARIES "pkgx::b"' in targets
    routes = {
        "in-tree": f'source_project = [["pkgx", "{PKGX / "project"}"]]\n',
        "cps": f'package_search_dir = [["cps", "{prefix}/share/cps"]]\n',
        "cmake": f'package_search_dir = [["cmake", "{prefix}/share/cmake"]]\n',
    }
    # The build's include dirs and where install_map puts them.
    tree_dirs = {
        str(PKGX / "project" / "a" / "include"): "include",
        str(PKGX / "project" / "b" / "include"): "include/pkgx",
    }
    got = {}
    for route, text in routes.items():
        u = tmp_path / f"{route}.toml"
        u.write_text(text)
        b = tmp_path / f"down-{route}"
        configure(PKGX / "downstream", b, [PRESETS / "apple-clang-arm64.toml", u])
        facts = usage(b, prefix)
        got[route] = {
            tuple("$ROOT/" + tree_dirs[x] if x in tree_dirs else x for x in f) for f in facts
        }
        ninja(b)
        subprocess.run([str(b / "souffle" / "bin" / REL / "app")], check=True)
    assert got["in-tree"] == {
        ("use", "cc.define", "A_API=1", "1"),
        ("use", "cc.define", "B_API=2", "1"),
        ("use", "cc.include_dir", "$ROOT/include", "1"),
        ("use", "cc.include_dir", "$ROOT/include/pkgx", "1"),
        ("use_join", "cxx.std", "17"),
    }
    assert got["cps"] == got["in-tree"] and got["cmake"] == got["in-tree"]
    # The bare package name is its default components (a and b), in-tree as
    # from CPS; one prefix with CPS, CMake and buildlog metadata binds pkgx:a to
    # the buildlog manifest (the CPS file names it as its sibling: one
    # candidate; CMake's file only names the id).
    bare = tmp_path / "bare"
    shutil.copytree(PKGX / "downstream", bare, ignore=shutil.ignore_patterns("buildlog-build"))
    spec = bare / "spec.toml"
    spec.write_text(spec.read_text().replace('"pkgx:a"', '"pkgx"'))
    (bare / "buildlog.toml").write_text(
        (bare / "buildlog.toml").read_text() + f'source_root = "{PKGX / "downstream"}"\n'
    )
    both = tmp_path / "prefix-route.toml"
    both.write_text(f'package_prefix = [["{prefix}"]]\n')
    for route, u in [("in-tree", tmp_path / "in-tree.toml"), ("cps", tmp_path / "cps.toml")]:
        b = tmp_path / f"bare-{route}"
        configure(bare, b, [PRESETS / "apple-clang-arm64.toml", u])
        facts = usage(b, prefix)
        facts = {tuple("$ROOT/" + tree_dirs[x] if x in tree_dirs else x for x in f) for f in facts}
        assert facts == got["in-tree"], route
    b = tmp_path / "down-prefix"
    down = tmp_path / "down-prefix-src"
    shutil.copytree(
        PKGX / "downstream", down, ignore=shutil.ignore_patterns("buildlog-build", "buildlog.lock")
    )
    configure(down, b, [PRESETS / "apple-clang-arm64.toml", both], update_lock=True)
    selected = (b / "souffle" / "facts" / "import_select.csv").read_text()
    assert f"pkgx:a\tarm64-macos\tbuildlog\t{prefix}/share/buildlog/pkgx/manifest.toml" in selected
    assert usage(b, prefix) == got["in-tree"]


# ---- options are namespaced by project ------------------------------------------


def configure_errors(project: Path, build: Path, users: list[Path]) -> list:
    try:
        configure_project(project, build, user_files=users)
    except SystemExit:
        pass
    return [tuple(e) for e in json.loads((build / "metrics.json").read_text())["errors"]]


def test_source_project_options_are_namespaced(tmp_path):
    """A source project's option X is <name>.X: the user sets it qualified;
    the unqualified name is an unknown option that names the qualified one."""
    base = [DOWN / "presets" / "apple-clang-arm64.toml", DOWN / "presets" / "in-tree.toml"]
    bare = tmp_path / "bare.toml"
    bare.write_text('option_value = [["JSON_Diagnostics", "on"]]\n')
    errors = configure_errors(DOWN, tmp_path / "bare", base + [bare])
    assert (
        "unknown option",
        "JSON_Diagnostics (a source project's option: nlohmann_json.JSON_Diagnostics)",
    ) in errors
    b = downstream(tmp_path, "in-tree", "diagnostics")
    eff = (b / "souffle" / "facts" / "option_eff.csv").read_text().splitlines()
    assert "nlohmann_json.JSON_Diagnostics\ton" in eff


@pytest.mark.parametrize("decides", [False, True])
def test_author_may_not_set_a_source_project_option(tmp_path, decides):
    """Only the user sets a source project's option; [author_decides] is no
    bypass."""
    proj = tmp_path / "down"
    shutil.copytree(DOWN, proj, ignore=shutil.ignore_patterns("buildlog-build", "presets"))
    config = proj / "buildlog.toml"
    config.write_text(
        config.read_text().replace('source_root = ".."', f'source_root = "{DOWN.parent}"')
        + ('\n[author_decides]\noption_value = "the author wants diagnostics"\n' if decides else "")
    )
    config.write_text(
        'option_value = [["nlohmann_json.JSON_Diagnostics", "on"]]\n' + config.read_text()
    )
    users = [DOWN / "presets" / "apple-clang-arm64.toml", tmp_path / "in-tree.toml"]
    users[1].write_text(f'source_project = [["nlohmann_json", "{PROJECT}"]]\n')
    with pytest.raises(SpecError, match="source project's option; only the user sets it"):
        configure_project(proj, tmp_path / "b", user_files=users)


def test_source_project_conditions_do_not_clash(tmp_path):
    """The downstream may define a condition with the same name as one of the
    old source-project conditions: the source project's option conditions are
    qualified."""
    user = tmp_path / "cond.toml"
    user.write_text('condition = [["diagnostics", "$Eq(native.opt, debug)"]]\n')
    b = downstream(tmp_path, "in-tree", "diagnostics")
    names = {
        r.split("\t")[1]
        for r in (b / "souffle" / "facts" / "cond_holds.csv").read_text().splitlines()
    }
    assert "nlohmann_json.JSON_Diagnostics" in names and "diagnostics" not in names
    errors = configure_errors(
        DOWN,
        tmp_path / "clash",
        [
            DOWN / "presets" / "apple-clang-arm64.toml",
            DOWN / "presets" / "in-tree.toml",
            DOWN / "presets" / "nlohmann-diagnostics.toml",
            user,
        ],
    )
    assert errors == []


# ---- export formats: lossiness and opting out -----------------------------------


def test_lossy_format_is_a_fact_and_a_warning(tmp_path):
    """pkg-config cannot state cxx.std: export_lossy says so (why-able) and
    configure warns, naming the relations that drop the format."""
    build = tmp_path / "b"
    metrics = configure(PROJECT, build, [PRESETS / "apple-clang-arm64.toml"])
    lossy = (build / "souffle" / "facts" / "export_lossy.csv").read_text().splitlines()
    reason = "pkg-config has no field for a language standard"
    assert lossy == [f"nlohmann_json\tpc\tnlohmann_json\tcxx.std\t{reason}"]
    ((kind, detail),) = metrics["warnings"]
    assert kind == "export format cannot state a usage requirement"
    assert detail.startswith(f"nlohmann_json:nlohmann_json cxx.std in pc ({reason})")
    fact = f'export_lossy("nlohmann_json", "pc", "nlohmann_json", "cxx.std", "{reason}")'
    out = subprocess.run(
        ["uv", "run", "--frozen", "buildlog", "why", "-C", str(build), "--leaves", fact]
        + [str(PROJECT)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'target_interface("nlohmann_json", "cxx.std", "11")' in out
    assert f'export_unspelled("pc", "cxx.std", "{reason}")  ← plugins/buildlog-cpp' in out


def test_user_skips_an_export_format(tmp_path):
    """The user (or packager) drops pkg-config: no .pc is written or
    installed, and nothing is lossy any more."""
    prefix, build = tmp_path / "usr", tmp_path / "b"
    user = tmp_path / "skip.toml"
    user.write_text(
        f'install_prefix = "{prefix}"\nexport_format_skip = [["nlohmann_json", "pc"]]\n'
    )
    metrics = configure(PROJECT, build, [PRESETS / "apple-clang-arm64.toml", user])
    assert metrics["warnings"] == []
    ninja(build, "install")
    assert not (prefix / "share" / "pkgconfig").exists()
    assert (prefix / "share" / "cps" / "nlohmann_json.cps").is_file()
    assert (prefix / "share" / "cmake" / "nlohmann_json" / "nlohmann_jsonTargets.cmake").is_file()


def test_author_omits_an_export_format(tmp_path):
    """The author omits the CMake files (all three) with a reason; an
    unknown format name is an error."""
    proj = tmp_path / "pkgx"
    shutil.copytree(PKGX / "project", proj, ignore=shutil.ignore_patterns("buildlog-build"))
    spec = proj / "spec.toml"
    omit = 'export_format_omit = [["pkgx", "cmake", "consumers use CPS"]]\n'
    spec.write_text(omit + spec.read_text())
    prefix, build = tmp_path / "usr", tmp_path / "b"
    user = tmp_path / "prefix.toml"
    user.write_text(f'install_prefix = "{prefix}"\n')
    configure(proj, build, [PRESETS / "apple-clang-arm64.toml", user])
    ninja(build, "install")
    assert not (prefix / "share" / "cmake").exists()
    assert {p.name for p in prefix.rglob("*.*")} >= {"pkgx.cps", "pkgx.pc"}
    spec.write_text(spec.read_text().replace('"cmake", "consumers', '"cmake-config", "consumers'))
    errors = configure_errors(proj, tmp_path / "b2", [PRESETS / "apple-clang-arm64.toml"])
    assert ("unknown export format", "pkgx: cmake-config") in errors


def test_export_key_both_spelled_and_unspelled_is_an_error(tmp_path):
    """A format that both spells a key and declares it unspelled would
    derive a spelling and a loss for one fact; the conflict is an error
    (likewise for a value-spelled key)."""
    proj = tmp_path / "pkgx"
    shutil.copytree(PKGX / "project", proj, ignore=shutil.ignore_patterns("buildlog-build"))
    toml = proj / "buildlog.toml"
    toml.write_text(
        toml.read_text().replace('["buildlog-cpp"]', '["buildlog-cpp", "export-conflict"]')
    )
    try:
        configure_project(
            proj,
            tmp_path / "b",
            user_files=[PRESETS / "apple-clang-arm64.toml"],
            package_paths=[CONFLICT],
            update_lock=True,
        )
    except SystemExit:
        pass
    errors = [tuple(e) for e in json.loads((tmp_path / "b" / "metrics.json").read_text())["errors"]]
    assert ("export key both spelled and unspelled", "pc: cc.define") in errors
    assert ("export key both spelled and unspelled", "cps: cxx.std") in errors
    assert ("export key both value-spelled and unspelled", "cps: cxx.std") in errors
