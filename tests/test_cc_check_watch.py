# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A check's cached answer is re-validated against what its compile read: the
headers it opened (edits, removals) and the search directories it could have
found one in (additions, shadowing), even when no environment variable changed."""

from __future__ import annotations

import os

import pytest

from buildlog.engine import configure_project

from .test_cpp_mechanisms import cmd_of, needs_souffle, project

pytestmark = [pytest.mark.integration, needs_souffle]

SPEC = """
cc_check_include = [["HAVE_WATCHED_H", "watched_probe.h"], ["HAVE_STDIO_H", "stdio.h"]]
prop_when = [
  ["app", "cc.define", "WATCHED", "private", "cc.check.HAVE_WATCHED_H"],
  ["app", "cc.define", "STDIO", "private", "cc.check.HAVE_STDIO_H"],
]
request = [["app", "rel"]]

[target.app]
kind = "executable"
sources = ["main.c"]
"""
MAIN = {"main.c": "int main(void) { return 0; }\n"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    inc = tmp_path / "inc"
    inc.mkdir()
    monkeypatch.setenv("CPATH", str(inc))
    proj = project(tmp_path, SPEC, MAIN)
    build = tmp_path / "build"

    def configure():
        _, metrics = configure_project(proj, build, update_lock=True)
        assert metrics["errors"] == []
        return metrics, cmd_of(build, "app/main.c.o")

    return inc, configure


def test_a_header_added_to_a_search_directory_is_seen(setup):
    inc, configure = setup
    _, cmd = configure()
    assert "-DWATCHED" not in cmd
    assert configure()[0]["executed"] == 0
    (inc / "watched_probe.h").write_text("/* new */\n")
    metrics, cmd = configure()
    assert "-DWATCHED" in cmd and metrics["executed"] >= 1


def test_a_header_removed_from_a_search_directory_is_seen(setup):
    inc, configure = setup
    header = inc / "watched_probe.h"
    header.write_text("/* here */\n")
    assert "-DWATCHED" in configure()[1]
    assert configure()[0]["executed"] == 0
    header.unlink()
    assert "-DWATCHED" not in configure()[1]


def test_a_header_edited_in_place_is_seen(setup):
    inc, configure = setup
    header = inc / "watched_probe.h"
    header.write_text("/* fine */\n")
    assert "-DWATCHED" in configure()[1]
    assert configure()[0]["executed"] == 0
    header.write_text("#error broken now, and longer\n")
    assert "-DWATCHED" not in configure()[1]


def test_an_unrelated_edit_in_a_search_directory_keeps_the_cache(setup):
    inc, configure = setup
    other = inc / "other.h"
    other.write_text("/* a */\n")
    (inc / "watched_probe.h").write_text("/* fine */\n")
    configure()
    other.write_text("/* b, edited in place */\n")
    st = other.stat()
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    metrics, cmd = configure()
    assert metrics["executed"] == 0
    assert "-DWATCHED" in cmd and "-DSTDIO" in cmd
