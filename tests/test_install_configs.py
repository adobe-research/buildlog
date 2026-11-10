# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Multi-configuration installs (L3_install.dl, "multi-configuration
installs"): several configurations
install into one prefix as long as nothing is clobbered. Identical content
to one destination is fine; different content is an error, at configure
time where it is known statically (export files) and at install time
otherwise (the `install` builtin hashes the sources of each destination).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog import builtins
from buildlog.builtins import InstallError
from buildlog.engine import configure_project
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
PKGX = ROOT / "tests" / "fixtures" / "export-components" / "project"
NLOHMANN = ROOT / "case-studies" / "nlohmann-json" / "project"
JSON_SRC = ROOT / "case-studies" / "nlohmann-json" / "upstream"
PIN = NLOHMANN / "presets" / "apple-clang-arm64.toml"
TWO_CONFIGS = """config_decl = [["release", "arm64-macos"], ["debug", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"], ["debug", "native.opt", "debug"]]
"""

E2E = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]


def e2e(fn):
    for mark in E2E:
        fn = mark(fn)
    return fn


def two_config_pkgx(tmp: Path, extra: str = "") -> Path:
    """pkgx with a release and a debug configuration (and `extra` spec)."""
    proj = tmp / "pkgx"
    shutil.copytree(PKGX, proj, ignore=shutil.ignore_patterns("buildlog-build"))
    spec = proj / "spec.toml"
    text = spec.read_text()
    text = text.replace('config_decl = [["release", "arm64-macos"]]\n', "")
    text = text.replace('config_decl_setting = [["release", "native.opt", "release"]]\n', "")
    spec.write_text(TWO_CONFIGS + extra + text)
    return proj


def configure(project: Path, build: Path, prefix: Path, **kw) -> dict:
    user = build.parent / f"{build.name}-prefix.toml"
    user.write_text(f'install_prefix = "{prefix}"\n')
    try:
        configure_project(project, build, user_files=[PIN, user], **kw)
    except SystemExit:
        pass
    return json.loads((build / "metrics.json").read_text())


def ninja(build: Path, *targets: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ninja", "-C", str(build / "souffle"), *targets],
        capture_output=True,
        text=True,
        check=False,
    )


def files(prefix: Path) -> dict[str, bytes]:
    return {str(p.relative_to(prefix)): p.read_bytes() for p in prefix.rglob("*") if p.is_file()}


# ---- the builtin -------------------------------------------------------------


def test_install_builtin_identical_sources_install_once(tmp_path):
    a, b = tmp_path / "a.h", tmp_path / "b.h"
    a.write_text("same\n")
    b.write_text("same\n")
    prefix = tmp_path / "usr"
    dest = f"{prefix}/include/x.h"
    builtins.install("--prefix", str(prefix), "--file", str(a), dest, "--file", str(b), dest)
    assert (prefix / "include" / "x.h").read_text() == "same\n"


def test_install_builtin_different_sources_fail_before_copying(tmp_path):
    a, b, c = tmp_path / "a.h", tmp_path / "b.h", tmp_path / "c.h"
    a.write_text("one\n")
    b.write_text("two\n")
    c.write_text("fine\n")
    prefix = tmp_path / "usr"
    dest = f"{prefix}/include/x.h"
    argv = ["--prefix", str(prefix), "--file", str(c), f"{prefix}/include/c.h"]
    argv += ["--file", str(a), dest, "--file", str(b), dest]
    with pytest.raises(InstallError, match=f"different contents for {dest}"):
        builtins.install(*argv)
    assert not prefix.exists()


# ---- end to end ----------------------------------------------------------------


@pytest.mark.skipif(not (JSON_SRC / "include").exists(), reason="nlohmann/json checkout absent")
@e2e
def test_nlohmann_release_and_debug_install_once(tmp_path):
    """Header-only: the two configurations' headers are the same files and
    their export files are identical, so both install into one prefix, and
    the tree is the one-configuration tree."""
    proj = tmp_path / "nlohmann"
    shutil.copytree(NLOHMANN, proj, ignore=shutil.ignore_patterns("buildlog-build"))
    # The spec is buildlog.toml's own (top-level rows before any table).
    spec = proj / "buildlog.toml"
    text = spec.read_text()
    text = text.replace('config_decl = [["release", "arm64-macos"]]\n', TWO_CONFIGS)
    text = text.replace('config_decl_setting = [["release", "native.opt", "release"]]\n', "")
    assert "debug" in text
    spec.write_text(text)
    two, one = tmp_path / "two", tmp_path / "one"
    metrics = configure(proj, tmp_path / "b2", two)
    assert metrics["errors"] == []
    assert ninja(tmp_path / "b2", "install").returncode == 0
    assert configure(NLOHMANN, tmp_path / "b1", one)["errors"] == []
    assert ninja(tmp_path / "b1", "install").returncode == 0
    assert files(two) == files(one)
    args = (tmp_path / "b2" / "souffle" / "build.ninja").read_text()
    assert "export/arm64-macos.release.none/nlohmann_json/" in args
    assert "export/arm64-macos.debug.none/nlohmann_json/" in args


@e2e
def test_export_differing_by_configuration_is_a_configure_error(tmp_path):
    """An exported define that holds only in debug: the two configurations'
    CPS files differ, so installing both is a configure-time error of the
    install action, naming the fact. With --keep-going the rest of the graph
    builds and only the install edge fails."""
    extra = 'condition = [["dbg", "$Eq(native.opt, debug)"]]\n'
    extra += 'prop_when = [["b", "cc.define", "B_DEBUG=1", "interface", "dbg"]]\n'
    proj = two_config_pkgx(tmp_path, extra)
    build = tmp_path / "b"
    metrics = configure(proj, build, tmp_path / "usr", keep_going=True)
    kinds = {tuple(e) for e in metrics["errors"]}
    assert (
        "two configurations export different files to one destination",
        (
            "share/cps/pkgx.cps: arm64-macos.debug.none has b definitions B_DEBUG=1, "
            "arm64-macos.release.none does not"
        ),
    ) in kinds
    assert ninja(build).returncode == 0
    r = ninja(build, "install")
    assert r.returncode != 0 and "two configurations export different files" in r.stdout
    assert not (tmp_path / "usr").exists()


@e2e
@pytest.mark.parametrize("same", [True, False])
def test_two_files_for_one_destination_checked_at_install(tmp_path, same):
    """Release installs one header and debug another at one destination:
    allowed when the contents agree, an install-time error otherwise."""
    proj = two_config_pkgx(tmp_path)
    for cfg in ("rel", "dbg"):
        d = proj / "extra" / cfg / "include"
        d.mkdir(parents=True)
        (d / "extra.h").write_text(
            "#define EXTRA 1\n" if same or cfg == "rel" else "#define EXTRA 2\n"
        )
    spec = proj / "spec.toml"
    spec.write_text(
        'condition = [["rel", "$Eq(native.opt, release)"], ["dbg", "$Eq(native.opt, debug)"]]\n'
        'install_file_when = [["b", "src:extra/rel/include/extra.h", "rel"], '
        '["b", "src:extra/dbg/include/extra.h", "dbg"]]\n'
        + spec.read_text().replace(
            'install_map = [["src:b/include", "includedir", "pkgx"]]',
            'install_map = [["src:b/include", "includedir", "pkgx"], '
            '["src:extra/rel/include", "includedir", ""], '
            '["src:extra/dbg/include", "includedir", ""]]',
        )
    )
    build, prefix = tmp_path / "b", tmp_path / "usr"
    assert configure(proj, build, prefix)["errors"] == []
    r = ninja(build, "install")
    if same:
        assert r.returncode == 0, r.stdout
        assert (prefix / "include" / "extra.h").read_text() == "#define EXTRA 1\n"
    else:
        assert r.returncode != 0
        assert f"different contents for {prefix}/include/extra.h" in r.stdout
        assert not prefix.exists()
