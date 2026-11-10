# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Meson normalizer (normalize_meson.py).

They use the miniature Meson-shaped fixture in fixtures/mini (fake roots
/work/src and /work/build: a native table generator, the custom target that
runs it, and a shared library and an executable both named `fribidi`) and never
need Meson. The last test re-normalizes the real release baseline and is skipped
unless baseline/raw/meson-release exists (run-meson.sh).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE = os.path.dirname(HERE)
MINI = os.path.join(HERE, "fixtures", "mini")


def _load():
    spec = importlib.util.spec_from_file_location(
        "fribidi_normalize_meson", os.path.join(BASELINE, "normalize_meson.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


nm = _load()


@pytest.fixture(scope="module")
def recs():
    out = nm.normalize_meson(
        "/work/build",
        "/work/src",
        "mini",
        build_ninja=os.path.join(MINI, "build.ninja"),
        meson_info=os.path.join(MINI, "meson-info"),
    )
    return {r["id"]: r for r in out}


def test_custom_command_is_unwrapped_to_the_tool_it_runs(recs):
    r = recs["$BUILD/gen.tab/x.tab.i"]
    assert r["kind"] == "custom"
    assert r["tool"] == "$BUILD/gen.tab/gen-tab" and r["tool_built"] is True
    (step,) = r["steps"]
    assert step["argv"] == [
        "$BUILD/gen.tab/gen-tab",
        "2",
        "$SRC/gen.tab/unidata/Data.txt",
        "gen-tab",
    ]
    assert step["stdout"] == "$BUILD/gen.tab/x.tab.i"
    assert step["wrapper"] == ["$HOMEBREW/bin/meson", "--internal", "exe"]
    # The original command is kept next to its decomposition.
    assert r["argv"][:5] == [
        "$HOMEBREW/bin/meson",
        "--internal",
        "exe",
        "--capture",
        step["stdout"],
    ]


def test_ccache_is_a_wrapper_not_the_tool(recs):
    r = recs["$BUILD/lib/libfribidi.0.dylib.p/fribidi.c.o"]
    assert r["kind"] == "compile" and r["tool_name"] == "cc"
    assert r["steps"][0]["wrapper"] == ["$HOMEBREW/bin/ccache"]


def test_build_relative_include_dirs_are_canonical(recs):
    argv = recs["$BUILD/lib/libfribidi.0.dylib.p/fribidi.c.o"]["steps"][0]["argv"]
    assert [a for a in argv if a.startswith("-I")] == [
        "-I$BUILD/lib/libfribidi.0.dylib.p",
        "-I$BUILD/lib",
        "-I$SRC/lib",
        "-I$BUILD",
        "-I$SRC",
        "-I$BUILD/gen.tab",
    ]


def test_targets_come_from_introspection_with_colliding_names_split(recs):
    got = {
        i: (r.get("target"), r.get("target_type"), r.get("machine"), r["dir"])
        for i, r in recs.items()
        if not r.get("excluded") and r["kind"] != "phony"
    }
    assert got == {
        "$BUILD/gen.tab/gen-tab.p/gen-tab.c.o": ("gen-tab@exe", "executable", "build", "gen.tab"),
        "$BUILD/gen.tab/gen-tab": ("gen-tab@exe", "executable", "build", "gen.tab"),
        "$BUILD/gen.tab/x.tab.i": ("gen-tab@cus", "custom", None, "gen.tab"),
        "$BUILD/lib/libfribidi.0.dylib.p/fribidi.c.o": (
            *("fribidi@sha", "shared library", "host", "lib"),
        ),
        "$BUILD/lib/libfribidi.0.dylib": ("fribidi@sha", "shared library", "host", "lib"),
        "$BUILD/bin/fribidi.p/fribidi-main.c.o": ("fribidi@exe", "executable", "host", "bin"),
        "$BUILD/bin/fribidi": ("fribidi@exe", "executable", "host", "bin"),
    }
    assert recs["$BUILD/lib/libfribidi.0.dylib"]["link_type"] == "shared"
    assert recs["$BUILD/bin/fribidi"]["link_type"] == "executable"


def test_meson_bookkeeping_is_excluded(recs):
    assert recs["$BUILD/build.ninja"]["excluded"] == "meson bookkeeping (regenerate)"
    sym = recs["$BUILD/lib/libfribidi.0.dylib.p/libfribidi.0.dylib.symbols"]
    assert sym["excluded"] == "meson bookkeeping" and sym["target"] == "fribidi@sha"
    assert "steps" not in sym  # excluded records are identity only


def test_no_machine_paths(recs):
    text = json.dumps(list(recs.values()))
    assert not re.search(r"/work/|/Users/|/opt/homebrew", text)


RAW = os.path.join(BASELINE, "raw", "meson-release")


@pytest.mark.skipif(not os.path.isdir(RAW), reason="run-meson.sh's raw release build is absent")
def test_checked_in_release_baseline_is_reproducible():
    src = os.path.expanduser(
        os.environ.get("BUILDLOG_FRIBIDI_SRC", os.path.join(BASELINE, "..", "upstream"))
    )
    out = nm.normalize_meson(RAW, src, "release")
    with open(os.path.join(BASELINE, "meson-release.jsonl")) as f:
        assert [json.loads(line) for line in f] == json.loads(json.dumps(out))
