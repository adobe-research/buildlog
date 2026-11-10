# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-platform install layout (buildlog-cpp L4_install.dl, "libdir depends
on the target platform"): GNUInstallDirs' libdir rule over platform facts.
Linux platforms come from the test-linux rule package in
tests/fixtures/layout (buildlog-cpp's inventory knows only macOS).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "layout"
NLOHMANN = ROOT / "case-studies" / "nlohmann-json" / "project"

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


def project(tmp: Path, distro: str | None, platforms=(("x86_64-linux", "x86_64"),)) -> Path:
    proj = tmp / "proj"
    shutil.copytree(FIXTURE / "project", proj, ignore=shutil.ignore_patterns("buildlog.lock"))
    spec = proj / "spec.toml"
    text = spec.read_text().splitlines(keepends=True)
    rows = ", ".join(f'["{p}", "{c}-unknown-linux-gnu", "{c}"]' for p, c in platforms)
    head = [f"test_linux_platform = [{rows}]\n"]
    if distro:
        head.append(
            "test_linux_distro = ["
            + ", ".join(f'["{p}", "{distro}"]' for p, _ in platforms)
            + "]\n"
        )
    decls = ", ".join(f'["r{i}", "{p}"]' for i, (p, _) in enumerate(platforms))
    sets = ", ".join(f'["r{i}", "native.opt", "release"]' for i in range(len(platforms)))
    head += [f"config_decl = [{decls}]\n", f"config_decl_setting = [{sets}]\n"]
    body = [line for line in text if not line.startswith(("test_linux_platform", "config_decl"))]
    spec.write_text("".join(head + body))
    return proj


def configure(proj: Path, build: Path, prefix: str, extra: str = "") -> tuple[dict, list]:
    user = build.parent / f"{build.name}-user.toml"
    user.write_text(f'install_prefix = "{prefix}"\n{extra}')
    try:
        configure_project(proj, build, package_paths=[FIXTURE], user_files=[user])
    except SystemExit:
        pass
    errors = [tuple(e) for e in json.loads((build / "metrics.json").read_text())["errors"]]
    csv = build / "souffle" / "facts" / "install_layout_dir.csv"
    layout = dict(line.split("\t") for line in csv.read_text().splitlines()) if not errors else {}
    return layout, errors


@pytest.mark.parametrize(
    ("distro", "prefix", "libdir"),
    [
        (None, "/usr", "lib64"),
        ("fedora", "/usr", "lib64"),
        ("debian", "/usr", "lib/x86_64-linux-gnu"),
        ("ubuntu", "/", "lib/x86_64-linux-gnu"),
        ("debian", "/opt/pkg", "lib"),
        ("arch", "/usr", "lib"),
    ],
)
def test_linux_libdir_default(tmp_path, distro, prefix, libdir):
    layout, errors = configure(project(tmp_path, distro), tmp_path / "b", prefix)
    assert errors == []
    assert layout["libdir"] == libdir
    assert layout["includedir"] == "include"


def test_linux_libdir_installs_there(tmp_path):
    """The install edge follows the layout: <prefix>/lib64/h/h.txt."""
    build = tmp_path / "b"
    configure(project(tmp_path, None), build, str(tmp_path / "pfx"))
    ninja = (build / "souffle" / "build.ninja").read_text()
    assert "lib64/h/h.txt" in ninja
    assert "include/h.h" in ninja


def test_platforms_that_disagree_need_install_dir(tmp_path):
    """Two target platforms with different defaults: an error until the
    user states libdir (one prefix, one layout)."""
    proj = project(tmp_path, None, platforms=(("x86_64-linux", "x86_64"), ("i686-linux", "i686")))
    _, errors = configure(proj, tmp_path / "b", "/usr")
    assert (
        "install directory has two defaults; state install_dir",
        "libdir: lib and lib64",
    ) in errors
    layout, errors = configure(
        proj, tmp_path / "c", "/usr", 'install_dir = [["libdir", "lib32"]]\n'
    )
    assert errors == []
    assert layout["libdir"] == "lib32"


def test_user_overrides_platform_default(tmp_path):
    layout, errors = configure(
        project(tmp_path, "debian"), tmp_path / "b", "/usr", 'install_dir = [["libdir", "lib"]]\n'
    )
    assert errors == []
    assert layout["libdir"] == "lib"


def test_why_shows_the_rule_inputs(tmp_path):
    """`why` explains the default from the platform's facts."""
    proj = project(tmp_path, "debian")
    build = tmp_path / "b"
    configure(proj, build, "/usr")
    out = subprocess.run(
        [
            *("uv", "run", "--frozen", "buildlog", "why", "-C", str(build), "--leaves"),
            'install_layout_dir("libdir", "lib/x86_64-linux-gnu")',
            str(proj),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for leaf in (
        'test_linux_distro("x86_64-linux", "debian")',
        'libdir_family("debian", "multiarch")',
    ):
        assert leaf in out
    assert 'cpu_multiarch("x86_64", "x86_64-linux-gnu")' in out
    assert 'install_prefix("/usr")' in out


def test_macos_keeps_lib(tmp_path):
    build = tmp_path / "b"
    user = tmp_path / "prefix.toml"
    user.write_text(f'install_prefix = "{tmp_path / "pfx"}"\n')
    configure_project(
        NLOHMANN, build, user_files=[NLOHMANN / "presets" / "apple-clang-arm64.toml", user]
    )
    layout = dict(
        line.split("\t")
        for line in (build / "souffle" / "facts" / "install_layout_dir.csv")
        .read_text()
        .splitlines()
    )
    assert layout["libdir"] == "lib"
