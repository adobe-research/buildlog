# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""An exported component's requirement on an imported package
(L3_install.dl, "requirements on imported packages"). pkgy (tests/fixtures/export-requires) depends on pkgx:b,
imported from buildlog's install of tests/fixtures/export-components. The
export requires pkgx by its origin's name, at the author's stated minimum
version if any, else at the version found (>= found), in CPS, pkg-config
and CMake; `why` says which supplied the version.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
PKGX = ROOT / "tests" / "fixtures" / "export-components" / "project"
FIX = ROOT / "tests" / "fixtures" / "export-requires"
PRESET = ROOT / "case-studies" / "nlohmann-json" / "project" / "presets" / "apple-clang-arm64.toml"
REL = "arm64-macos.release.none"
CFG = '["arm64-macos", [["native.opt", "release"], [["native.sanitize", "none"], [["native.linkage", "default"], nil]]]]'

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]


def configure(project: Path, build: Path, text: str) -> None:
    user = build.parent / f"{build.name}-user.toml"
    user.write_text(text)
    _, metrics = configure_project(project, build, user_files=[PRESET, user])
    assert metrics["errors"] == []


def ninja(build: Path, *args: str) -> None:
    subprocess.run(["ninja", "-C", str(build / "souffle"), *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def pkgx(tmp_path_factory) -> Path:
    """buildlog's install of pkgx 1.2.3."""
    tmp = tmp_path_factory.mktemp("pkgx")
    prefix = tmp / "x"
    configure(PKGX, tmp / "b", f'install_prefix = "{prefix}"\n')
    ninja(tmp / "b", "install")
    return prefix


def pkgy_project(tmp: Path, extra: str = "") -> Path:
    """pkgy, with `extra` author rows."""
    if not extra:
        return FIX / "project"
    proj = tmp / "pkgy"
    shutil.copytree(
        FIX / "project", proj, ignore=shutil.ignore_patterns("buildlog-build", "buildlog.lock")
    )
    (proj / "spec.toml").write_text(extra + (proj / "spec.toml").read_text())
    return proj


def install_pkgy(tmp: Path, pkgx: Path, extra: str = "", bind: str = "") -> tuple[Path, Path]:
    prefix = tmp / "y"
    build = tmp / "by"
    bind = bind or f'package_search_dir = [["cps", "{pkgx}/share/cps"]]\n'
    configure(pkgy_project(tmp, extra), build, f'install_prefix = "{prefix}"\n{bind}')
    ninja(build, "install")
    return build, prefix


def exported(prefix: Path) -> dict:
    return {
        "cps": json.loads((prefix / "share" / "cps" / "pkgy.cps").read_text()),
        "pc": (prefix / "share" / "pkgconfig" / "pkgy.pc").read_text(),
        "config": (prefix / "share" / "cmake" / "pkgy" / "pkgyConfig.cmake").read_text(),
        "targets": (prefix / "share" / "cmake" / "pkgy" / "pkgyTargets.cmake").read_text(),
    }


def version_from(build: Path) -> set[tuple[str, ...]]:
    with open(build / "souffle" / "facts" / "export_requires_version_from.csv") as f:
        return {(r[0], r[2], r[3], r[4]) for r in csv.reader(f, delimiter="\t")}


def why_leaves(build: Path, project: Path, fact: str) -> str:
    return subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "buildlog",
            "why",
            "-C",
            str(build),
            "--leaves",
            fact,
            str(project),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.parametrize("route", ["cps", "cmake"])
def test_requires_found_version(tmp_path, pkgx, route):
    """No stated version: pkgx is required at >= the version found (1.2.3),
    named by the origin (CPS `name`, CMake's pkgxConfig.cmake), in every
    format; `why` traces the version to the imported file."""
    bind = ""
    if route == "cmake":
        bind = f'package_search_dir = [["cmake", "{pkgx}/share/cmake"]]\n'
    build, prefix = install_pkgy(tmp_path, pkgx, bind=bind)
    out = exported(prefix)
    assert out["cps"]["requires"] == {"pkgx": {"version": "1.2.3"}}
    assert out["cps"]["components"]["y"]["requires"] == ["pkgx:b"]
    assert "Requires: pkgx >= 1.2.3\n" in out["pc"]
    assert "include(CMakeFindDependencyMacro)\nfind_dependency(pkgx 1.2.3)\n" in out["config"]
    assert 'INTERFACE_LINK_LIBRARIES "pkgx::b"' in out["targets"]
    assert version_from(build) == {("pkgy", "pkgx", "1.2.3", "found")}
    leaves = why_leaves(
        build,
        FIX / "project",
        f'export_requires_version_from("pkgy", {CFG}, "pkgx", "1.2.3", "found")',
    )
    assert "av_pkg_version(" in leaves
    assert ("pkgx.cps:" if route == "cps" else "pkgxConfigVersion.cmake:") in leaves
    assert 'dep("y", "pkgx:b", "public")' in leaves


def test_requires_stated_version_and_name(tmp_path, pkgx):
    """The author's minimum wins over the found version, and the author's
    name over the origin's; `why` traces the version to the author."""
    extra = (
        'export_requires_version = [["pkgy", "pkgx:b", "1.0"]]\n'
        'export_requires_name = [["pkgy", "pkgx:b", "PkgX"]]\n'
    )
    build, prefix = install_pkgy(tmp_path, pkgx, extra)
    out = exported(prefix)
    assert out["cps"]["requires"] == {"PkgX": {"version": "1.0"}}
    assert out["cps"]["components"]["y"]["requires"] == ["PkgX:b"]
    assert "Requires: PkgX >= 1.0\n" in out["pc"]
    assert "find_dependency(PkgX 1.0)\n" in out["config"]
    assert 'INTERFACE_LINK_LIBRARIES "PkgX::b"' in out["targets"]
    assert version_from(build) == {("pkgy", "PkgX", "1.0", "author")}
    leaves = why_leaves(
        build,
        tmp_path / "pkgy",
        f'export_requires_version_from("pkgy", {CFG}, "PkgX", "1.0", "author")',
    )
    assert 'export_requires_version("pkgy", "pkgx:b", "1.0")' in leaves
    assert "res_pkg_version(" not in leaves


def usage(build: Path, roots: dict[str, Path]) -> set[tuple[str, ...]]:
    out = set()
    real = {k: os.path.realpath(v) for k, v in roots.items()}
    for rel in ("use", "use_join"):
        with open(build / "souffle" / "facts" / f"{rel}.csv") as f:
            for row in csv.reader(f, delimiter="\t"):
                if row[0] != "app":
                    continue
                vals = []
                for v in row[2:]:
                    if v.startswith("/"):
                        v = os.path.normpath(os.path.realpath(v))
                        for k, r in real.items():
                            if v == r or v.startswith(r + "/"):
                                v = k + v[len(r) :]
                    vals.append(v)
                out.add((rel, *vals))
    return out


def test_downstream_gets_the_requirement(tmp_path, pkgx):
    """A downstream of pkgy:y, with pkgy imported (CPS) or built in-tree,
    derives the same usage facts, pkgx's included: the requirement crosses
    the install boundary. pkg-config and CMake consumers get pkgx too."""
    _, ypfx = install_pkgy(tmp_path, pkgx)
    down = FIX / "downstream"
    routes = {
        "cps": f'package_search_dir = [["cps", "{pkgx}/share/cps"], ["cps", "{ypfx}/share/cps"]]\n',
        "in-tree": (
            f'package_search_dir = [["cps", "{pkgx}/share/cps"]]\nsource_project = [["pkgy", "{FIX / "project"}"]]\n'
        ),
    }
    roots = {"$X": pkgx, "$Y": ypfx, "$Y_SRC": FIX / "project"}
    got = {}
    for route, text in routes.items():
        b = tmp_path / f"down-{route}"
        configure(down, b, text)
        got[route] = {
            tuple(v.replace("$Y_SRC/include", "$Y/include") for v in f) for f in usage(b, roots)
        }
        ninja(b)
        subprocess.run([str(b / "souffle" / "bin" / REL / "app")], check=True)
    assert got["cps"] == {
        ("use", "cc.define", "Y_API=5", "1"),
        ("use", "cc.define", "B_API=2", "1"),
        ("use", "cc.include_dir", "$Y/include", "1"),
        ("use", "cc.include_dir", "$X/include/pkgx", "1"),
        ("use_join", "cxx.std", "17"),
    }
    assert got["in-tree"] == got["cps"]

    if shutil.which("pkg-config"):
        env = dict(os.environ)
        env["PKG_CONFIG_PATH"] = f"{ypfx}/share/pkgconfig:{pkgx}/share/pkgconfig"
        flags = subprocess.run(
            ["pkg-config", "--cflags", "pkgy >= 0.1"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert "-DY_API=5" in flags and "-DB_API=2" in flags
        env["PKG_CONFIG_PATH"] = f"{ypfx}/share/pkgconfig"
        missing = subprocess.run(
            ["pkg-config", "--cflags", "pkgy"], env=env, capture_output=True, check=False
        )
        assert missing.returncode != 0

    if shutil.which("cmake"):
        src = tmp_path / "cmake-src"
        src.mkdir()
        shutil.copy(down / "main.cpp", src / "main.cpp")
        (src / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\nproject(app CXX)\n"
            "find_package(pkgy 0.1 CONFIG REQUIRED)\n"
            "add_executable(app main.cpp)\ntarget_link_libraries(app PRIVATE pkgy::y)\n"
        )
        b = tmp_path / "cmake-build"
        subprocess.run(
            ["cmake", "-S", str(src), "-B", str(b), "-G", "Ninja"]
            + [f"-DCMAKE_PREFIX_PATH={ypfx};{pkgx}"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["cmake", "--build", str(b)], check=True, capture_output=True)
        subprocess.run([str(b / "app")], check=True)
        # Without pkgx, find_dependency fails pkgy's find_package.
        bad = subprocess.run(
            ["cmake", "-S", str(src), "-B", str(tmp_path / "cmake-bad"), "-G", "Ninja"]
            + [f"-DCMAKE_PREFIX_PATH={ypfx}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert bad.returncode != 0 and "pkgx" in bad.stderr
