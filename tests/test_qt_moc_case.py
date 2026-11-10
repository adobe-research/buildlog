# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Qt case study (case-studies/qt-moc/README.md): Qt ships a rule package
that runs moc and rcc for whoever compiles against QtCore.

The integration tests need a real Qt 6 install ($BUILDLOG_QT_PREFIX, no
default: the tests skip when it is unset), e.g. `aqt install-qt mac desktop 6.11.3
clang_64 -O DIR`, then DIR/6.11.3/macos (Linux: `aqt install-qt linux_arm64 desktop 6.11.3
linux_gcc_arm64`, then DIR/6.11.3/gcc_arm64; the image's $BUILDLOG_QT_PREFIX). They "install" the rule package into a copy of
that prefix (symlinks to its lib, libexec and plugins), as Qt would ship
it, and configure a copy of the consumer, so neither tree is written.
The loader tests (PACKAGE_PREFIX, quoted table headers) need no Qt.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path

from . import host

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "qt-moc"
PACKAGES = CASE / "share" / "buildlog"
PROJECT = CASE / "project"
PRESET = PROJECT / "presets" / ("apple-clang.toml" if host.MACOS else f"{host.FAMILY}.toml")
QT = Path(os.path.expanduser(os.environ.get("BUILDLOG_QT_PREFIX") or "<BUILDLOG_QT_PREFIX unset>"))
HOST_PLATFORM = "arm64-macos" if host.MACOS else "arm64-linux"
EXEC = f"{HOST_PLATFORM}.release.none"
# macOS also builds x86_64 and universal2; Linux has the host's two configurations only.
CONFIGS = None if host.MACOS else ["debug", "release"]
EXEC_CONFIGS = 2 if host.MACOS else 1

needs_qt = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (QT / "libexec" / "moc").exists(),
        reason=f"no Qt 6 at {QT}: set BUILDLOG_QT_PREFIX (aqt install-qt mac desktop 6.11.3 clang_64 -O DIR; DIR/6.11.3/macos)",
    ),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]


def _needs(fn):
    for m in needs_qt:
        fn = m(fn)
    return fn


def _qt_prefix(root: Path) -> Path:
    """Qt's prefix with the rule package installed, as Qt would ship it."""
    prefix = root / "qt"
    (prefix / "share" / "buildlog").mkdir(parents=True)
    for d in ("lib", "libexec", "plugins", "include", "mkspecs", "bin"):
        (prefix / d).symlink_to(QT / d)
    shutil.copytree(PACKAGES, prefix / "share" / "buildlog", dirs_exist_ok=True)
    return prefix


def _project(root: Path, extra: str, files: dict[str, str]) -> Path:
    proj = root / "project"
    shutil.copytree(PROJECT, proj, ignore=shutil.ignore_patterns("buildlog.lock", "buildlog-build"))
    cfg = proj / "buildlog.toml"
    # Request the extra target too, then append its table.
    text = cfg.read_text()
    if "[target.plain]" in extra:
        text = text.replace(
            'request_all = [["counter_test"]]',
            'request_all = [["counter_test"], ["plain"], ["excl"]]',
        )
    cfg.write_text(text + extra)
    for rel, text in files.items():
        (proj / rel).write_text(text)
    return proj


def _configure(root: Path, extra: str = "", files: dict | None = None) -> tuple[Path, Path, Path]:
    prefix, proj, build = _qt_prefix(root), _project(root, extra, files or {}), root / "build"
    _, metrics = configure_project(
        proj,
        build,
        package_paths=[prefix],
        var_overrides={"QT_PREFIX": str(prefix)},
        user_files=[PRESET],
        configs=CONFIGS,
        update_lock=True,
    )
    assert metrics["errors"] == []
    return prefix, proj, build


def _commands(build: Path, label: str) -> list[str]:
    text = (build / "souffle" / "build.ninja").read_text()
    return re.findall(rf"  label = {label}\n  cmd = (.*)", text)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("qt")
    # A target that does not use Qt: its header is never given to moc.
    # A Qt user that excludes its one header: moc never sees it.
    plain = (
        '\n[target.plain]\nkind = "library"\nsources = ["src/plain.cpp", "src/plain.h"]\n'
        '\n[target.excl]\nkind = "library"\nsources = ["src/excl.cpp", "src/excl.h"]\n'
        'deps = { public = ["Qt6::Core"] }\nqt_moc_exclude = ["src/excl.h"]\n'
    )
    files = {
        "src/plain.h": "#pragma once\nint plain();\n",
        "src/plain.cpp": '#include "plain.h"\nint plain() { return 1; }\n',
        "src/excl.h": "#pragma once\nint excl();\n",
        "src/excl.cpp": '#include "excl.h"\nint excl() { return 2; }\n',
    }
    prefix, proj, build = _configure(root, plain, files)
    subprocess.run(["ninja", "-C", str(build / "souffle"), "-j2"], check=True, capture_output=True)
    return prefix, proj, build


@_needs
def test_linking_qtcore_runs_moc_on_the_targets_headers(built):
    _, _, build = built
    mocs = _commands(build, "MOC")
    # counter's two headers, in each of the single-platform configurations
    # (macOS: debug, release and x86_64; universal2's slices are release's and
    # x86_64's); plain's header and counter_test (no headers) get none.
    made = sorted(re.search(r"-o (\S+)", c).group(1).split("/", 1)[1] for c in mocs)
    assert made == sorted(
        f"{cfg}/counter/src/moc_{h}.cpp"
        for cfg in (
            (f"{HOST_PLATFORM}.debug.none", EXEC)
            + (("x86_64-macos.release.none",) if host.MACOS else ())
        )
        for h in ("counter", "version")
    )
    assert not any("plain.h" in c or "excl.h" in c for c in mocs)
    assert (build / "souffle" / "lib" / EXEC / "libexcl.a").exists()
    assert (build / "souffle" / "lib" / EXEC / "libplain.a").exists()
    # moc is Qt's host tool, run where the build runs whatever the product
    # targets; it reads the header with the target's defines and includes.
    assert all(c.split()[0].endswith("/libexec/moc") for c in mocs)
    assert all("-DQT_CORE_LIB" in c and "--output-dep-file" in c for c in mocs)
    assert sum("-DQT_NO_DEBUG" in c for c in mocs) == 2 * EXEC_CONFIGS
    # A header without Q_OBJECT gives an empty file, which compiles to nothing.
    out = build / "souffle" / "gen"
    assert (out / EXEC / "counter" / "src" / "moc_version.cpp").read_text() == ""
    assert "staticMetaObject" in (out / EXEC / "counter" / "src" / "moc_counter.cpp").read_text()


@_needs
def test_rcc_embeds_the_resource_list_into_its_target(built):
    _, _, build = built
    rccs = _commands(build, "RCC")
    assert len(rccs) == 1 + EXEC_CONFIGS and all("--name=greeting" in c for c in rccs)
    objs = (build / "souffle" / "obj" / EXEC / "counter_test").rglob("qrc_greeting.cpp.o")
    assert len(list(objs)) == 1


@_needs
def test_the_consumer_runs_headless_natively_and_builds_for_x86_64_and_universal2(built):
    """On Linux only the native run: the cross slices are macOS's."""
    _, _, build = built
    res = subprocess.run(
        ["uv", "run", "--frozen", "buildlog", "test", "-C", str(build), "--no-build"],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    not_run = 1 if host.MACOS else 0
    assert f"2 passed, 0 skipped, 0 failed, {not_run} not run" in res.stdout, (
        res.stdout + res.stderr
    )
    if not host.MACOS:
        return
    exe = build / "souffle" / "bin" / "x86_64-macos.release.none" / "counter_test"
    arch = subprocess.run(
        ["file", "-b", str(exe)], capture_output=True, text=True, check=True
    ).stdout
    assert "x86_64" in arch
    fat = build / "souffle" / "bin" / "universal2-macos.release.none" / "counter_test"
    archs = subprocess.run(
        ["lipo", "-archs", str(fat)], capture_output=True, text=True, check=True
    ).stdout.split()
    assert sorted(archs) == ["arm64", "x86_64"]
    subprocess.run(
        [str(fat)],
        check=True,
        capture_output=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )


@_needs
def test_why_the_moc_output_is_compiled_into_counter(built):
    prefix, proj, build = built
    res = subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "buildlog",
            "why",
            "-C",
            str(build),
            "--package-path",
            str(prefix),
        ]
        + ["--output", "moc_counter.cpp.o", "--flag", "moc_counter.cpp"]
        + ["--config", "debug", str(proj)],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout
    lines = res.splitlines()
    assert len(lines) <= 20, res
    # The rule (Qt6Core's package), the edge to QtCore and the header (the
    # consumer's buildlog.toml), and QtCore's definition: Qt's own export file.
    assert "qt_gen(" in res and "[Qt6Core:Qt6Core.dl:" in res
    assert re.search(
        r'target_deps\("counter", "public", "Qt6::Core"\)  ← \S*buildlog.toml:\d+', res
    )
    assert re.search(r'target_sources\("counter", "src/counter.h"\)  ← \S*buildlog.toml:\d+', res)
    assert re.search(
        r'res_pkg_target\("[^"]*", "Qt6::Core", "library"\)  ← \S*lib/cmake/Qt6Core/Qt6CoreTargets.cmake:59',
        res,
    )


@_needs
def test_an_exclusion_must_name_a_source_of_its_target(tmp_path, capsys):
    prefix, proj = _qt_prefix(tmp_path), _project(tmp_path, "", {})
    cfg = proj / "buildlog.toml"
    cfg.write_text(
        cfg.read_text().replace(
            'deps = { public = ["Qt6::Core"] }',
            'deps = { public = ["Qt6::Core"] }\nqt_moc_exclude = ["src/nope.h"]',
        )
    )
    with pytest.raises(SystemExit):
        configure_project(
            proj,
            tmp_path / "build",
            package_paths=[prefix],
            var_overrides={"QT_PREFIX": str(prefix)},
            user_files=[PRESET],
            configs=CONFIGS,
            update_lock=True,
        )
    err = capsys.readouterr().err
    assert "qt_moc_exclude names no source of its target" in err and "counter: src/nope.h" in err


# --- loader: what the package relies on, without Qt ---


def test_package_prefix_is_where_the_package_is_installed(tmp_path):
    from buildlog.facts import package_vars

    pkg = tmp_path / "pfx" / "share" / "buildlog" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "manifest.toml").write_text('name = "demo"\n')
    (pkg / "sub").mkdir()
    for facts in (pkg / "spec.toml", pkg / "sub" / "more.toml"):
        assert package_vars(facts, {"X": "1"}) == {
            "X": "1",
            "PACKAGE_PREFIX": str((tmp_path / "pfx").resolve()),
        }
    # A package outside <prefix>/share/buildlog (a project's packages/) has none.
    local = tmp_path / "proj" / "packages" / "demo"
    local.mkdir(parents=True)
    (local / "manifest.toml").write_text('name = "demo"\n')
    assert package_vars(local / "spec.toml", {"X": "1"}) == {"X": "1"}


def test_quoted_subject_table_cites_its_header_line(tmp_path):
    from buildlog.facts import _table_key_line, _unquote_header

    assert _unquote_header('[imported."Qt6::Core"]') == "[imported.Qt6::Core]"
    lines = ["# x", '[imported."Qt6::Core"]', "prop = []", '[imported."Qt6::Gui"]', "dep = []"]
    assert _table_key_line(lines, "imported.Qt6::Gui", "dep", 1) == 5
