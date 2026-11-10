# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run-time dependencies and per-configuration file names: runtime_dep(t, m, place) builds m in t's configuration, never
links it, orders the loader after it and puts it where the loader looks;
native_file_name(t, c, f) is the artifact's file, written by the link."""

from __future__ import annotations

import re
import subprocess

import pytest

from buildlog.errors import SpecError
from tests import host
from tests.test_cpp_mechanisms import (
    configure,
    configure_fails,
    configure_project,
    needs_souffle,
    ninja_text,
    project,
    run_ninja,
)

REL = "arm64-macos.release.none"

# loader (a shared library) dlopens every name it is given from the
# directory it is told; app links loader and asks for its own directory
# (dirname of a fresh copy each time: glibc's rewrites its argument).
FILES = {
    "plug.c": "int plug_value(void) { return PLUG_VALUE; }\n",
    "loader.c": (
        "#include <dlfcn.h>\n#include <stdio.h>\n"
        "int load(const char* dir, const char* name) {\n"
        '  char p[4096]; snprintf(p, sizeof p, "%s/%s", dir, name);\n'
        "  void* h = dlopen(p, RTLD_NOW); if (!h) { puts(dlerror()); return -1; }\n"
        '  int (*f)(void) = (int (*)(void))dlsym(h, "plug_value"); return f();\n}\n'
    ),
    "main.c": (
        "#include <libgen.h>\n#include <stdio.h>\n#include <string.h>\n"
        "int load(const char*, const char*);\n"
        "int main(int argc, char** argv) {\n"
        "  for (int i = 1; i < argc; i++) {\n"
        "    char d[4096]; strncpy(d, argv[0], sizeof d - 1); d[sizeof d - 1] = 0;\n"
        '    printf("%d\\n", load(dirname(d), argv[i]));\n  }\n'
        "  return 0;\n}\n"
    ),
}

SPEC = """
target = [["loader"], ["app"], ["plug_a"], ["plug_b"]]
native_kind = [["loader", "library"], ["app", "executable"],
  ["plug_a", "module_library"], ["plug_b", "module_library"]]
default_linkage = [["loader", "shared"]]
c_source = [["loader", "loader.c"], ["app", "main.c"], ["plug_a", "plug.c"], ["plug_b", "plug.c"]]
prop = [["plug_a", "cc.define", "PLUG_VALUE=1", "private"], ["plug_b", "cc.define", "PLUG_VALUE=2", "private"]]
dep = [["app", "loader", "private"]]
request = [["app", "rel"]]
"""


def build_lines(build):
    return [ln for ln in ninja_text(build).splitlines() if ln.startswith("build ")]


def line_of(build, output):
    return next(ln for ln in build_lines(build) if ln.split(":")[0].split()[1] == output)


@needs_souffle
def test_modules_beside_executables_are_built_linked_beside_and_loaded(tmp_path):
    proj = project(
        tmp_path,
        SPEC
        + """runtime_dep = [["loader", "plug_a", "beside_executables"], ["loader", "plug_b", "beside_executables"]]
""",
        FILES,
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    text = ninja_text(build)
    # Built because the loader needs them, in its configuration; never linked.
    assert (
        f"lib/{REL}/libplug_a.so" in text and "-lplug" not in text and "libplug_a.so -" not in text
    )
    loader = line_of(build, f"lib/{REL}/{host.shlib('loader')}")
    assert f"|| lib/{REL}/libplug_a.so lib/{REL}/libplug_b.so" in loader
    # A link beside every executable that links the loader, ordered before it.
    link = line_of(build, f"bin/{REL}/libplug_a.so")
    assert f"lib/{REL}/libplug_a.so" in link
    app = line_of(build, f"bin/{REL}/app")
    assert f"|| bin/{REL}/libplug_a.so bin/{REL}/libplug_b.so" in app
    subprocess.run(
        ["ninja", "-C", str(build / "souffle"), f"bin/{REL}/app"], check=True, capture_output=True
    )
    exe = build / "souffle" / "bin" / REL / "app"
    assert (exe.parent / "libplug_b.so").is_symlink()
    got = subprocess.check_output([str(exe), "libplug_a.so", "libplug_b.so"], text=True)
    assert got == "1\n2\n"


@needs_souffle
def test_beside_the_loader_needs_no_link_and_a_condition_gates_the_edge(tmp_path):
    proj = project(
        tmp_path,
        SPEC
        + """runtime_dep = [["loader", "plug_a", "beside_loader"]]
runtime_dep_when = [["loader", "plug_b", "beside_loader", "never"]]
condition = [["never", "$Eq(native.opt, debug)"]]
""",
        FILES,
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    text = ninja_text(build)
    assert "plug_b" not in text
    assert f"bin/{REL}/libplug_a.so" not in text
    assert f"|| lib/{REL}/libplug_a.so" in line_of(build, f"lib/{REL}/{host.shlib('loader')}")
    run_ninja(build)
    out = build / "souffle" / "lib" / REL
    assert (out / "libplug_a.so").is_file()


@needs_souffle
def test_an_executable_loader_gets_its_modules_beside_it(tmp_path):
    proj = project(
        tmp_path,
        SPEC
        + """runtime_dep = [["app", "plug_a", "beside_loader"]]
""",
        FILES,
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert f"|| bin/{REL}/libplug_a.so" in line_of(build, f"bin/{REL}/app")


@needs_souffle
def test_runtime_dep_errors(tmp_path, capsys):
    cases = [
        ('runtime_dep = [["loader", "plug_a", "nearby"]]', "unknown run-time dependency place"),
        (
            'runtime_dep = [["loader", "plug_a", "beside_loader"]]\ndep = [["loader", "plug_a", "private"]]',
            "run-time dependency is also linked",
        ),
        (
            'runtime_dep = [["loader", "nothing", "beside_loader"]]',
            "run-time dependency nothing of loader",
        ),
        (
            (
                'runtime_dep = [["loader", "objs", "beside_loader"]]\ntarget = [["objs"]]\n'
                'native_kind = [["objs", "object_library"]]\nc_source = [["objs", "plug.c"]]'
            ),
            "run-time dependency has no file to load",
        ),
    ]
    for i, (extra, message) in enumerate(cases):
        base = tmp_path / str(i)
        base.mkdir()
        spec = SPEC
        # A second `dep`/`target` table would redefine the key: merge rows.
        for key in ("dep", "target", "native_kind", "c_source"):
            line = next((x for x in extra.splitlines() if x.startswith(f"{key} = ")), None)
            if line:
                rows = line.split(" = ", 1)[1][1:-1]
                spec = spec.replace(f"{key} = [", f"{key} = [{rows}, ", 1)
                extra = extra.replace(line, "")
        proj = project(base, spec + extra + "\n", FILES)
        err = configure_fails(proj, base, capsys)
        assert message in err, (message, err)


NAMING_MANIFEST = """\
name = "naming"
version = "0.1.0"
rank = 5
namespace = "naming"
layers = ["naming.dl"]
requires = { buildlog-cpp = ">=0.1" }
extends = ["native_file_named", "native_file_name"]
"""
NAMING_DL = """\
native_file_named("plug_a"). native_file_named("app").
native_file_name("plug_a", c, cat("plug_a.", n, ".so")) :- needed("plug_a", c), cfg_name(c, n).
native_file_name("app", c, "app-renamed") :- needed("app", c).
"""


@needs_souffle
def test_native_file_name_is_what_the_link_writes(tmp_path):
    proj = project(
        tmp_path,
        SPEC + 'runtime_dep = [["loader", "plug_a", "beside_executables"]]\n',
        FILES,
        packages={"naming": (NAMING_MANIFEST, NAMING_DL)},
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    f = f"plug_a.{REL}.so"
    assert f"-o lib/{REL}/{f}" in ninja_text(build)
    assert f"|| bin/{REL}/{f}" in line_of(build, f"bin/{REL}/app-renamed")
    run_ninja(build)
    exe = build / "souffle" / "bin" / REL / "app-renamed"
    assert subprocess.check_output([str(exe), f], text=True) == "1\n"


@needs_souffle
@pytest.mark.parametrize(
    ("dl", "message"),
    [
        (
            'native_file_named("loader").\nnative_file_name("loader", c, "loader.so") :- needed("loader", c).\n',
            "native file name for a kind without one file",
        ),
        ('native_file_named("plug_a").\n', "no native file name"),
        (
            'native_file_name("app", c, "x") :- needed("app", c).\n',
            "native file name of a target not so named",
        ),
        (
            'native_file_named("app").\nnative_file_name("app", c, "a/b") :- needed("app", c).\n',
            "native file name is a path",
        ),
    ],
)
def test_native_file_name_errors(tmp_path, capsys, dl, message):
    spec = SPEC + 'runtime_dep = [["loader", "plug_a", "beside_loader"]]\n'
    proj = project(tmp_path, spec, FILES, packages={"naming": (NAMING_MANIFEST, dl)})
    err = configure_fails(proj, tmp_path, capsys)
    assert message in err


# ---- the same facts as per-target TOML tables ----
# `[target.t] runtime_dep = ...` is the core relation's own spelling: a key k in
# [target.t] states k(t, ...). The module comes first, then the place.

TABLE_SPEC = """
request_all = [["loader"], ["app"]]
install_target = [["loader", "libdir"], ["app", "bindir"]]

[target.loader]
kind = "library"
default_linkage = "shared"
sources = ["loader.c"]
{loader}

[target.app]
kind = "executable"
sources = ["main.c"]
deps = {{ private = ["loader"] }}

[target.plug_a]
kind = "module_library"
sources = ["plug.c"]
private = {{ "cc.define" = "PLUG_VALUE=1" }}

[target.plug_b]
kind = "module_library"
sources = ["plug.c"]
private = {{ "cc.define" = "PLUG_VALUE=2" }}
"""


def table_install(root, capsys, loader, facts=None):
    """Configure the table spec with an install prefix: (build lines, installed paths)."""
    proj = project(root, TABLE_SPEC.format(loader=loader), FILES)
    if facts:
        (proj / "rules.dl").write_text(facts)
        toml = proj / "buildlog.toml"
        toml.write_text(
            toml.read_text().replace('spec = ["spec.toml"]', 'spec = ["spec.toml", "rules.dl"]')
        )
    (proj / "user.toml").write_text(f'install_prefix = "{root / "pfx"}"\n')
    capsys.readouterr()
    build = root / "build"
    _, metrics = configure_project(proj, build, user_files=[proj / "user.toml"], update_lock=True)
    assert metrics["errors"] == []
    assert capsys.readouterr().err == ""
    installed = (build / "souffle" / "facts" / "installed.csv").read_text().splitlines()
    lines = [ln for ln in build_lines(build) if "build.ninja" not in ln.split(":")[0]]
    return lines, sorted(r.split("\t")[2] for r in installed)


def plugs(installed):
    return sorted(n for n in installed if "plug" in n)


@needs_souffle
def test_target_table_states_required_and_optional_run_time_deps(tmp_path, capsys):
    lines, installed = table_install(
        tmp_path,
        capsys,
        'runtime_dep = { plug_a = "beside_executables" }\n'
        'runtime_dep_optional = [["plug_b", "beside_executables"]]',
    )
    # Both are built and placed beside app; only the required one installs.
    app = next(ln for ln in lines if ln.split(":")[0].split()[1] == f"bin/{REL}/app")
    assert f"bin/{REL}/libplug_a.so" in app and f"bin/{REL}/libplug_b.so" in app
    assert plugs(installed) == ["bin/libplug_a.so"]


@needs_souffle
def test_target_table_conditions_gate_both_strengths(tmp_path, capsys):
    lines, installed = table_install(
        tmp_path,
        capsys,
        'runtime_dep_when = [["plug_a", "beside_loader", "native.opt == release"]]\n'
        'runtime_dep_optional_when = [["plug_b", "beside_loader", "native.opt == debug"]]',
    )
    text = "\n".join(lines)
    assert f"lib/{REL}/libplug_a.so" in text
    assert "plug_b" not in text
    assert plugs(installed) == ["lib/libplug_a.so"]


@needs_souffle
def test_target_table_form_equals_the_datalog_form(tmp_path, capsys):
    table = (
        'runtime_dep = { plug_a = "beside_executables" }\n'
        'runtime_dep_optional = [["plug_b", "beside_loader"]]\n'
        'runtime_dep_when = [["plug_b", "beside_executables", "native.opt == debug"]]\n'
        'runtime_dep_optional_when = [["plug_a", "beside_loader", "native.opt == release"]]'
    )
    datalog = (
        'runtime_dep("loader", "plug_a", "beside_executables").\n'
        'runtime_dep_optional("loader", "plug_b", "beside_loader").\n'
        'runtime_dep_when("loader", "plug_b", "beside_executables", "native.opt == debug").\n'
        'runtime_dep_optional_when("loader", "plug_a", "beside_loader", "native.opt == release").\n'
    )
    got = []
    for name, loader, facts in (("toml", table, None), ("dl", "", datalog)):
        root = tmp_path / name
        root.mkdir()
        lines, installed = table_install(root, capsys, loader, facts)
        got.append(([ln.replace(str(root), "@") for ln in lines], installed))
    assert got[0] == got[1]
    assert plugs(got[0][1]) == ["bin/libplug_a.so"]


@pytest.mark.parametrize(
    "loader, message",
    [
        ('runtime_dep = { plug_a = "nearby" }', "unknown run-time dependency place"),
        ('runtime_dep_optional = [["plug_a", "nearby"]]', "unknown run-time dependency place"),
        (
            'runtime_dep_optional_when = [["plug_a", "nearby", "native.opt == release"]]',
            "unknown run-time dependency place",
        ),
    ],
)
@needs_souffle
def test_target_table_unknown_place_is_an_error(tmp_path, capsys, loader, message):
    proj = project(tmp_path, TABLE_SPEC.format(loader=loader), FILES)
    err = configure_fails(proj, tmp_path, capsys)
    assert message in err


@pytest.mark.parametrize(
    "loader, message",
    [
        ('runtime_dep = ["plug_a"]', "runtime_dep does not have values for all non-number columns"),
        (
            'runtime_dep_when = [["plug_a", "beside_loader"]]',
            "runtime_dep_when does not have values for all non-number columns",
        ),
        ('runtime_dep = [["plug_a", "beside_loader", "extra"]]', "runtime_dep has too many values"),
        (
            'runtime_deps = { plug_a = "beside_loader" }',
            "runtime_deps is not a relation; did you mean runtime_dep?",
        ),
    ],
)
def test_target_table_malformed_rows_are_load_errors(tmp_path, loader, message):
    proj = project(tmp_path, TABLE_SPEC.format(loader=loader), FILES)
    with pytest.raises(SpecError, match=re.escape(message)):
        configure_project(proj, tmp_path / "build", emit=False, update_lock=True)
