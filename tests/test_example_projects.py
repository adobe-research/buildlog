# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The runnable example projects: each project
configures, and builds where it is a stand-in for a buildable project; the
walkthrough's contradictory user file is rejected with its error."""

import subprocess
from pathlib import Path

import pytest

from buildlog.paths import souffle_path

from . import host

ROOT = Path(__file__).resolve().parents[1]
LISTINGS = ROOT / "examples"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    # the listings declare the arm64-macos platform and build dylibs
    host.macos_only("the listings declare arm64-macos configurations"),
]


def buildlog(*args, check=True):
    return subprocess.run(
        ["uv", "run", "--frozen", "buildlog", *map(str, args)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def configure(project, build, *extra, check=True):
    return buildlog("configure", *extra, "-C", build, LISTINGS / project, check=check)


def ninja(build):
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True, capture_output=True)


def built(build, pattern):
    return sorted(p.name for p in (build / "souffle").glob(pattern))


def test_surface_configures_and_builds(tmp_path):
    build = tmp_path / "surface"
    configure("surface", build)
    ninja(build)
    assert "liblibfribidi.a" in built(build, "lib/*/*")
    assert "gen-bidi-type-tab" in built(build, "bin/*/*")
    assert "fribidi-cli" not in built(build, "bin/*/*")


def test_surface_tools_option_adds_the_cli(tmp_path):
    build = tmp_path / "surface-tools"
    configure("surface", build, "-O", "TOOLS=on")
    ninja(build)
    assert "fribidi-cli" in built(build, "bin/*/*")


def test_template_instance_builds_as_a_module(tmp_path):
    build = tmp_path / "template"
    configure("template", build)
    ninja(build)
    assert "libkernels-avx2.so" in built(build, "lib/*/*")


def test_walkthrough_default_linkage_is_shared_codec_and_static_filters(tmp_path):
    build = tmp_path / "b0"
    configure("walkthrough", build)
    ninja(build)
    libs = built(build, "lib/*/*")
    assert "libcodec.dylib" in libs
    assert "libfilters.a" in libs
    assert "viewer" in built(build, "bin/*/*")


def test_walkthrough_user_static_codec_and_why_leaves(tmp_path):
    build = tmp_path / "b1"
    configure("walkthrough", build, "--user", LISTINGS / "walkthrough" / "user-static.toml")
    ninja(build)
    libs = built(build, "lib/*/*")
    assert "libcodec.a" in libs
    assert "libcodec.dylib" not in libs
    why = buildlog(
        "why",
        "-C",
        build,
        "--flag",
        "-DCODEC_STATIC",
        "--output",
        "viewer.c.o",
        "--leaves",
        LISTINGS / "walkthrough",
    ).stdout
    # the root tuple, then the eleven leaves the paper counts
    lines = [line for line in why.splitlines() if line.strip()]
    assert lines[0].startswith("action_arg(")
    assert len(lines) - 1 == 11
    assert 'library_linkage("codec", "static")' in why
    assert "user-static.toml:1" in why


def test_walkthrough_user_shared_contradicts_the_author_requirement(tmp_path):
    cp = configure(
        "walkthrough",
        tmp_path / "b2",
        "--user",
        LISTINGS / "walkthrough" / "user-shared.toml",
        check=False,
    )
    assert cp.returncode == 1
    assert "user linkage contradicts the library's requirement" in cp.stderr
    assert "filters" in cp.stderr and "static" in cp.stderr
