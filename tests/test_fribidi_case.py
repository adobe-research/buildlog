# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""fribidi case study: the buildlog build of fribidi v1.0.17 against its Meson build.

The project is case-studies/fribidi/project, configured with its preset user
file against the pinned fribidi checkout (buildlog.toml's FRIBIDI_SRC, which
BUILDLOG_FRIBIDI_SRC overrides); the ground truth is case-studies/fribidi/
baseline (run-meson.sh's normalized output of fribidi's own Meson build,
--buildtype=release). The differences these tests allow are the ones the
README's ledger explains.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from buildlog.engine import configure_project
from buildlog.paths import souffle_path
from tests import host

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "case-studies" / "fribidi"
PROJECT = CASE / "project"
PRESET = PROJECT / "presets" / "apple-clang-arm64.toml"
BASELINE = host.baseline_dir(CASE)
SRC = Path(os.path.expanduser(os.environ.get("BUILDLOG_FRIBIDI_SRC", str(CASE / "upstream"))))
REL = "arm64-macos.release.none"
TABLES = ["arabic-shaping", "bidi-type", "brackets", "brackets-type", "joining-type", "mirroring"]
GEN_TOOLS = ["gen-unicode-version"] + [f"gen-{t}-tab" for t in TABLES]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(
        not (SRC / "gen.tab" / "unidata" / "UnicodeData.txt").exists(),
        reason="the pinned fribidi checkout is absent",
    ),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    build = tmp_path_factory.mktemp("fribidi") / "build"
    _, metrics = configure_project(PROJECT, build, user_files=[PRESET])
    assert metrics["errors"] == []
    subprocess.run(["ninja", "-C", str(build / "souffle"), "-j2"], check=True, capture_output=True)
    return build


def _commands(build: Path) -> list[str]:
    return subprocess.run(
        ["ninja", "-C", str(build / "souffle"), "-t", "commands"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


# Meson target labels (normalize_meson.labels) -> buildlog target names.
MESON_TARGET = {"fribidi@sha": "libfribidi", "fribidi@exe": "fribidi"} | {
    f"{t}@exe": t for t in GEN_TOOLS[1:]
}


def _flags(argv: list[str], resolve) -> dict:
    """Defines, source include dirs (relative to the source root; generated
    ones resolve to None and are skipped) and other flags of a compile's argv."""
    defines, src_dirs, other = set(), set(), set()
    it = iter(argv[1:])
    for w in it:
        if w in ("-isystem",):
            w = "-I" + next(it)
        if w.startswith(("-D", "-U")):
            defines.add(w)
        elif w.startswith("-I"):
            rel = resolve(w[2:])
            if rel is not None:
                src_dirs.add(rel)
        elif w in ("-o", "-c", "-MQ", "-MF", "-isysroot"):
            next(it)
        elif w != "-MD":
            other.add(w)
    return {"defines": defines, "src_dirs": src_dirs, "other": other}


def meson_compiles() -> dict[tuple[str, str], dict]:
    out = {}
    for line in (BASELINE / "meson-release.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["kind"] != "compile":
            continue
        argv = r["steps"][0]["argv"]
        src = argv[argv.index("-c") + 1].removeprefix("$SRC/")

        def resolve(d):
            return None if d.startswith("$BUILD") else os.path.normpath(d.removeprefix("$SRC/"))

        target = MESON_TARGET.get(r["target"], r["target"])
        out[(target, src)] = _flags(argv, lambda d: "." if d == "$SRC" else resolve(d)) | {
            "target_dir": r["dir"]
        }
    return out


def buildlog_compiles(build: Path) -> dict[tuple[str, str], dict]:
    cwd = build / "souffle"
    src_root = SRC.resolve()

    def resolve(d):
        p = (cwd / d).resolve()
        return os.path.relpath(p, src_root) if p.is_relative_to(src_root) else None

    out = {}
    for line in _commands(build):
        argv = shlex.split(line)
        if "-c" not in argv:
            continue
        o = argv[argv.index("-o") + 1]
        target = re.search(r"obj/[^/]+/([^/]+)/", o).group(1)
        src = resolve(argv[argv.index("-c") + 1])
        out[(target, src)] = _flags(argv, resolve)
    return out


def test_the_same_objects_with_the_same_defines_and_source_include_dirs(build):
    ours, theirs = buildlog_compiles(build), meson_compiles()
    # 42 objects: packtab.c once per table generator, as in Meson.
    assert set(ours) == set(theirs) and len(ours) == 42
    for key, meson in theirs.items():
        mine = ours[key]
        # Ledger: buildlog's release configuration defines NDEBUG (Meson's
        # b_ndebug defaults to false).
        assert mine["defines"] - {"-DNDEBUG"} == meson["defines"], key
        # The source directories searched: Meson's, less its implicit
        # per-target directory (implicit_include_directories) where incs does
        # not list it.
        implicit = {meson["target_dir"]} - {".", "lib", "gen.tab"}
        assert mine["src_dirs"] == meson["src_dirs"] - implicit, key
        # Ledger: Meson's warning_level=1 and colour flags; buildlog's target
        # triple, sysroot, and -fPIC on shared-library objects.
        meson_other = meson["other"] - {"-fdiagnostics-color=always", "-Wall", "-Winvalid-pch"}
        # (On Linux Meson spells -fPIC on shared-library objects too, so it is compared.)
        mine_other = {
            w
            for w in mine["other"]
            if not w.startswith("--target=") and (w != "-fPIC" or not host.MACOS)
        }
        assert mine_other == meson_other, key


def test_generated_tables_are_byte_identical(build):
    import hashlib

    expected = dict(
        reversed(line.split())
        for line in (BASELINE / "release" / "tables.md5").read_text().splitlines()
    )
    gen = build / "souffle" / "gen" / REL
    got = {f"{t}.tab.i": gen / "tables" / f"{t}.tab.i" for t in TABLES}
    got["fribidi-unicode-version.h"] = gen / "unicode_version" / "fribidi-unicode-version.h"
    assert {k: hashlib.md5(p.read_bytes()).hexdigest() for k, p in got.items()} == expected


def test_table_generators_are_built_for_the_exec_configuration_as_tools(build):
    """No row makes the gen.tab programs host tools: they are needed only
    because the table actions run them (tool_dep), in libfribidi's exec
    configuration, which for this native build is the release configuration."""
    out = subprocess.run(
        [sys.executable, "-m", "buildlog", "why", "-C", str(build), "--output"]
        + [f"bin/{REL}/gen-bidi-type-tab", "--depth", "4", str(PROJECT)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert 'tool_dep("tables"' in out
    assert "exec_config(" in out
    ninja = (build / "souffle" / "build.ninja").read_text()
    for t in TABLES:
        edge = next(
            line
            for line in ninja.splitlines()
            if line.startswith(f"build gen/{REL}/tables/{t}.tab.i:")
        )
        assert f"bin/{REL}/gen-{t}-tab" in edge.split("|", 1)[1]


def _macros(text: str) -> dict[str, str]:
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r"^#define (\w+)(.*)$", text, re.MULTILINE)
    }


def test_config_h_defines_what_mesons_does(build):
    ours = (build / "souffle" / "gen" / REL / "config" / "config.h").read_text()
    theirs = (BASELINE / "release" / "config.h").read_text()
    assert _macros(ours) == _macros(theirs)


def test_fribidi_config_h_differs_only_in_its_generator_line(build):
    ours = (build / "souffle" / "gen" / REL / "config" / "fribidi-config.h").read_text()
    theirs = (BASELINE / "release" / "fribidi-config.h").read_text()
    assert ours.replace("by buildlog", "by Meson") == theirs


def test_library_exports_the_same_symbols(build):
    def names(text):
        return sorted(line.split()[-1] for line in text.splitlines() if line.strip())

    lib = build / "souffle" / "lib" / REL / host.shlib("fribidi", "0.4.0")
    nm = ["nm", "-gU"] if host.MACOS else ["nm", "-D", "--defined-only"]
    ours = subprocess.run([*nm, str(lib)], check=True, capture_output=True, text=True).stdout
    theirs = (BASELINE / "release" / "exported-symbols.txt").read_text()
    assert names(ours) == names(theirs) and len(names(ours)) == 36
    # Both install as @rpath/libfribidi.0.dylib (the ledger: file name and
    # current_version differ).
    if host.MACOS:
        otool = subprocess.run(
            ["otool", "-D", str(lib)], check=True, capture_output=True, text=True
        )
        assert otool.stdout.splitlines()[-1] == "@rpath/libfribidi.0.dylib"
    else:
        dyn = subprocess.run(
            ["readelf", "-d", str(lib)], check=True, capture_output=True, text=True
        )
        assert "Library soname: [libfribidi.so.0]" in dyn.stdout


def test_buildlog_test_runs_and_passes_meson_tests_set(build):
    out = build.parent / "results.json"
    subprocess.run(
        [sys.executable, "-m", "buildlog", "test", "-C", str(build), "-j2", "--json", str(out)],
        check=True,
        capture_output=True,
    )
    results = json.loads(out.read_text())
    meson = {t["name"] for t in json.loads((BASELINE / "release" / "intro-tests.json").read_text())}
    assert len(meson) == 9
    assert results["counts"] == {"passed": 9, "skipped": 0, "failed": 0, "not run": 0}
    assert {r["name"] for r in results["results"]} == meson


@pytest.fixture(scope="module")
def static(tmp_path_factory):
    """-Dfribidi:default_library=static: the user's per-library linkage."""
    root = tmp_path_factory.mktemp("fribidi-static")
    user = root / "static.toml"
    user.write_text('library_linkage = [["libfribidi", "static"]]\n')
    build = root / "build"
    _, metrics = configure_project(PROJECT, build, user_files=[PRESET, user])
    assert metrics["errors"] == []
    subprocess.run(["ninja", "-C", str(build / "souffle"), "-j2"], check=True, capture_output=True)
    return build


def test_static_linkage_is_the_users_and_carries_fribidi_lib_static(static):
    """Meson reads default_library to add -DFRIBIDI_LIB_STATIC to the library
    and its users; here it is a usage requirement of the static flavour."""
    lib = static / "souffle" / "lib" / REL
    assert (lib / "libfribidi.a").exists()
    assert not list(lib.glob(f"libfribidi*{host.SHLIB_SUFFIX}*"))
    compiles = [c for c in _commands(static) if " -c " in c]
    lib_objs = [c for c in compiles if f"obj/{REL}/libfribidi/" in c]
    prog_objs = [c for c in compiles if f"obj/{REL}/fribidi/" in c]
    assert lib_objs and prog_objs
    assert all("-DFRIBIDI_LIB_STATIC" in c for c in lib_objs + prog_objs)
    assert all("-fPIC" not in c for c in lib_objs)
    out = static.parent / "results.json"
    subprocess.run(
        [sys.executable, "-m", "buildlog", "test", "-C", str(static), "-j2", "--json", str(out)],
        check=True,
        capture_output=True,
    )
    assert json.loads(out.read_text())["counts"]["passed"] == 9


def test_the_default_shared_build_has_no_fribidi_lib_static(build):
    assert not any("FRIBIDI_LIB_STATIC" in c for c in _commands(build))


CROSS = PROJECT / "presets" / "cross-x86_64.toml"
X86 = "x86_64-macos.release.none"


@pytest.fixture(scope="module")
def cross(tmp_path_factory):
    host.require_macos()
    build = tmp_path_factory.mktemp("fribidi-cross") / "build"
    _, metrics = configure_project(PROJECT, build, user_files=[CROSS], configs=["x86_64-release"])
    assert metrics["errors"] == []
    subprocess.run(["ninja", "-C", str(build / "souffle"), "-j2"], check=True, capture_output=True)
    return build


def _archs(path: Path) -> str:
    return subprocess.run(
        ["lipo", "-archs", str(path)], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_cross_build_keeps_the_table_generators_on_the_exec_platform(cross):
    """The x86_64 configuration alone: the gen.tab programs are built once, for
    arm64 (the exec platform), the library and programs for x86_64 - what
    Meson's cross file and `native: true` give (meson-cross-x86_64.jsonl)."""
    out = cross / "souffle"
    for t in GEN_TOOLS:
        assert _archs(out / "bin" / REL / t) == "arm64"
        assert not (out / "bin" / X86 / t).exists()
    assert _archs(out / "lib" / X86 / "libfribidi.0.4.0.dylib") == "x86_64"
    assert _archs(out / "bin" / X86 / "fribidi") == "x86_64"
    assert not (out / "lib" / REL).exists()
    # The same 42 objects as Meson's cross build, split the same way between
    # the machines.
    meson = [
        json.loads(line)
        for line in (BASELINE / "meson-cross-x86_64.jsonl").read_text().splitlines()
    ]
    meson_build = {
        MESON_TARGET.get(r["target"], r["target"])
        for r in meson
        if r["kind"] == "compile" and r["machine"] == "build"
    }
    assert meson_build == set(GEN_TOOLS)
    ours = buildlog_compiles(cross)
    assert len(ours) == sum(r["kind"] == "compile" for r in meson) == 42
    arm64_objects = {
        re.search(rf"-o obj/{REL}/([^/]+)/", c).group(1)
        for c in _commands(cross)
        if f"-o obj/{REL}/" in c
    }
    assert arm64_objects == meson_build


def test_cross_tables_match_and_tests_are_not_run(cross):
    import hashlib

    expected = dict(
        reversed(line.split())
        for line in (BASELINE / "release" / "tables.md5").read_text().splitlines()
    )
    gen = cross / "souffle" / "gen" / X86
    got = {f"{t}.tab.i": gen / "tables" / f"{t}.tab.i" for t in TABLES}
    got["fribidi-unicode-version.h"] = gen / "unicode_version" / "fribidi-unicode-version.h"
    assert {k: hashlib.md5(p.read_bytes()).hexdigest() for k, p in got.items()} == expected
    # buildlog runs no x86_64 program on this arm64 machine (no emulator is
    # declared); Meson skips the three that run one directly (ledger).
    out = cross.parent / "results.json"
    subprocess.run(
        [sys.executable, "-m", "buildlog", "test", "-C", str(cross), "--json", str(out)],
        check=True,
        capture_output=True,
    )
    assert json.loads(out.read_text())["counts"] == {
        "passed": 0,
        "skipped": 0,
        "failed": 0,
        "not run": 9,
    }
