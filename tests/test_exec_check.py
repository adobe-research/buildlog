# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The exec inference check: an action's program
(argv[0]) that the build makes must be made for the build machine. A tool
named by reference (action_run) is, by construction; a rule that spells the
tool's path in the product's configuration (Bazel's tool in `srcs`, not
`tools`) is caught, and fails that action rather than the configuration."""

from __future__ import annotations

from buildlog.engine import configure_project
from tests import host
from tests.test_cpp_mechanisms import (
    EMBED_DL,
    EMBED_MANIFEST,
    configure_fails,
    needs_souffle,
    project,
)

# The mistake: emit is a dependency of app, and its path is app's configuration's.
BAD_DL = EMBED_DL.replace(
    'action_run(o, 0, "", g, "run") :- embedgen_out(_, _, o, g, _).',
    'dep(t, g, "private") :- embedgen_source(t, g, _).\n'
    'action_arg(o, 0, "", p), action_in(o, p, "implicit") :- embedgen_out(t, c, o, g, _), executable(g, c, p).',
)
BAD_MANIFEST = EMBED_MANIFEST.replace('"action_run",', '"action_arg", "action_in", "dep",')

FILES = {
    "main.cpp": '#include <cstdio>\nint value();\nint main() { std::printf("%d\\n", value()); }\n',
    "emit.cpp": '#include <cstdio>\nint main() { int v = 0; std::scanf("%d", &v);\n'
    '  std::printf("int value() { return %d; }\\n", v); }\n',
    "value.txt": "41\n",
}

SPEC = """
target = [["app"], ["emit"]]
native_kind = [["app", "executable"], ["emit", "executable"]]
cxx_source = [["app", "main.cpp"], ["emit", "emit.cpp"]]
embedgen_source = [["app", "emit", "value.txt"]]
"""


PINS = ", ".join(
    f'["{lang}", "arm64-macos", "{p}", "apple-clang"]'
    for lang in ("c", "cxx", "native-link")
    for p in ("x86_64-macos", "universal2-macos")
)


def _project(tmp_path, cfg, good=False):
    dl, manifest = (EMBED_DL, EMBED_MANIFEST) if good else (BAD_DL, BAD_MANIFEST)
    proj = project(
        tmp_path,
        SPEC + f'request = [["app", "{cfg}"]]\n',
        FILES,
        packages={"embedgen": (manifest, dl)},
    )
    spec = proj / "spec.toml"
    # the fixture pins the arm64-macos toolchains; pin the other platforms too
    text = spec.read_text().replace("toolchain_pin = [", f"toolchain_pin = [{PINS}, ", 1)
    text = text.replace(
        "config_decl = [", 'config_decl = [["x86", "x86_64-macos"], ["u2", "universal2-macos"], ', 1
    )
    spec.write_text(text)
    return proj


@host.cross
@needs_souffle
def test_a_tool_spelled_in_a_cross_products_configuration_fails_its_action(tmp_path, capsys):
    proj = _project(tmp_path, "x86")
    err = configure_fails(proj, tmp_path, capsys)
    assert "action runs a program built for another platform" in err
    assert "out:bin/x86_64-macos.debug.none/emit (emit in x86_64-macos.debug.none" in err
    assert "the build machine is arm64-macos" in err
    # Attributed: with keep_going the graph is emitted, the action failing.
    build = tmp_path / "kg"
    _, metrics = configure_project(proj, build, update_lock=True, keep_going=True)
    assert metrics["failed_actions"] >= 1
    lines = (build / "souffle" / "build.ninja").read_text().splitlines()
    assert "build gen/x86_64-macos.debug.none/app.cpp: fail" in lines


@host.cross
@needs_souffle
def test_a_tool_spelled_in_the_products_configuration_passes_natively(tmp_path):
    """The product's platform is the build machine's: nothing runs foreign."""
    proj = _project(tmp_path, "rel")
    _, metrics = configure_project(proj, tmp_path / "b", update_lock=True, emit=False)
    assert metrics["errors"] == []


@host.cross
@needs_souffle
def test_a_universal_product_fails_only_its_foreign_slice(tmp_path, capsys):
    """universal2 builds app per slice, so the x86_64 slice's generator runs
    the x86_64 emit (which Rosetta would hide); the arm64 slice's passes."""
    proj = _project(tmp_path, "u2")
    err = configure_fails(proj, tmp_path, capsys)
    assert "out:bin/x86_64-macos.debug.none/emit (emit in x86_64-macos.debug.none" in err
    assert "arm64-macos.debug.none/emit" not in err


@host.cross
@needs_souffle
def test_a_tool_named_by_reference_passes_in_a_cross_build(tmp_path):
    proj = _project(tmp_path, "x86", good=True)
    _, metrics = configure_project(proj, tmp_path / "b", update_lock=True, emit=False)
    assert metrics["errors"] == []
