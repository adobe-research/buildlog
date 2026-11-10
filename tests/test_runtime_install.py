# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Installing and exporting run-time modules: runtime_dep (required) installs
with its loader, runtime_dep_optional only if the author lists the module."""

from __future__ import annotations

import os
import subprocess

from buildlog import dl_package
from tests.test_cpp_mechanisms import configure_project, needs_souffle, project

FILES = {
    "lib.c": "int lib(void) { return 1; }\n",
    "mod.c": "int mod(void) { return 2; }\n",
    "main.c": "int main(void) { return 0; }\n",
}

SPEC = """
target = [["lib"], ["modA"], ["modB"], ["app"], ["app2"]]
native_kind = [["lib", "library"], ["modA", "module_library"], ["modB", "module_library"],
  ["app", "executable"], ["app2", "executable"]]
default_linkage = [["lib", "shared"]]
c_source = [["lib", "lib.c"], ["modA", "mod.c"], ["modB", "mod.c"], ["app", "main.c"], ["app2", "main.c"]]
dep = [["app", "lib", "private"], ["app2", "lib", "private"]]
request_all = [["lib"], ["app"], ["app2"]]
"""


def install(tmp_path, capsys, spec, base=SPEC):
    """Configure with an install prefix; (build dir, errors text, installed paths)."""
    proj = project(tmp_path, base + spec, FILES)
    (proj / "user.toml").write_text(f'install_prefix = "{tmp_path / "pfx"}"\n')
    capsys.readouterr()
    try:
        configure_project(
            proj, tmp_path / "build", user_files=[proj / "user.toml"], update_lock=True, emit=False
        )
    except SystemExit:
        pass
    err = capsys.readouterr().err
    path = tmp_path / "build" / "souffle" / "facts" / "installed.csv"
    got = sorted(r.split("\t")[2] for r in path.read_text().splitlines()) if path.exists() else []
    return tmp_path / "build", err, got


def modules(got):
    return sorted(n for n in got if "modA" in n or "modB" in n)


LIB = 'install_target = [["lib", "libdir"]]\n'


@needs_souffle
def test_required_beside_the_loader_installs_into_the_loaders_directory(tmp_path, capsys):
    spec = 'runtime_dep = [["lib", "modA", "beside_loader"]]\n' + LIB
    _, err, got = install(tmp_path, capsys, spec)
    assert err == ""
    assert modules(got) == ["lib/libmodA.so"]


@needs_souffle
def test_required_beside_executables_installs_beside_each_installed_one(tmp_path, capsys):
    spec = (
        'runtime_dep = [["lib", "modA", "beside_executables"]]\n'
        'install_target = [["app", "bindir"], ["app2", "bindir"]]\n'
    )
    _, err, got = install(tmp_path, capsys, spec)
    assert err == ""
    # Two programs in one directory: one file there, not a conflict.
    assert modules(got) == ["bin/libmodA.so"]


@needs_souffle
def test_required_beside_executables_in_two_directories_is_an_error(tmp_path, capsys):
    spec = (
        'runtime_dep = [["lib", "modA", "beside_executables"]]\n'
        'install_target = [["app", "bindir"], ["app2", "datadir"]]\n'
    )
    _, err, _got = install(tmp_path, capsys, spec)
    assert "required run-time module installs to two directories" in err
    assert "modA" in err and "bin and share" in err


@needs_souffle
def test_beside_executables_with_no_installed_executable_goes_beside_the_loader(tmp_path, capsys):
    spec = 'runtime_dep = [["lib", "modA", "beside_executables"]]\n' + LIB
    _, err, got = install(tmp_path, capsys, spec)
    assert err == ""
    assert modules(got) == ["lib/libmodA.so"]


@needs_souffle
def test_an_installed_executable_that_loads_by_itself_gets_its_module(tmp_path, capsys):
    spec = (
        'runtime_dep = [["app", "modA", "beside_loader"]]\ninstall_target = [["app", "bindir"]]\n'
    )
    _, err, got = install(tmp_path, capsys, spec)
    assert err == ""
    assert modules(got) == ["bin/libmodA.so"]


@needs_souffle
def test_an_optional_module_is_not_installed_unless_listed(tmp_path, capsys):
    spec = 'runtime_dep_optional = [["lib", "modA", "beside_loader"]]\n' + LIB
    build, err, got = install(tmp_path, capsys, spec)
    assert err == ""
    assert modules(got) == []
    # ... but it is still built and placed, as before.
    order = (build / "souffle" / "facts" / "action_in.csv").read_text()
    assert "libmodA.so\torder_only" in order


@needs_souffle
def test_an_optional_module_is_installed_when_listed(tmp_path, capsys):
    spec = (
        'runtime_dep_optional = [["lib", "modA", "beside_loader"]]\n'
        'install_target = [["lib", "libdir"], ["modA", "bindir"]]\n'
    )
    _, err, got = install(tmp_path, capsys, spec)
    assert err == ""
    assert modules(got) == ["bin/libmodA.so"]


@needs_souffle
def test_required_modules_are_installed_transitively(tmp_path, capsys):
    spec = (
        'runtime_dep = [["lib", "modA", "beside_loader"], ["modA", "modB", "beside_loader"]]\n'
        + LIB
    )
    _, err, got = install(tmp_path, capsys, spec)
    assert err == ""
    assert modules(got) == ["lib/libmodA.so", "lib/libmodB.so"]


@needs_souffle
def test_what_an_optional_module_requires_follows_it_only_when_it_is_installed(tmp_path, capsys):
    base = 'runtime_dep_optional = [["lib", "modA", "beside_loader"]]\nruntime_dep = [["modA", "modB", "beside_loader"]]\n'
    _, err, got = install(tmp_path, capsys, base + LIB)
    assert err == "" and modules(got) == []
    _, err, got = install(
        tmp_path / "listed",
        capsys,
        base + 'install_target = [["lib", "libdir"], ["modA", "bindir"]]\n',
    )
    assert err == ""
    assert modules(got) == ["bin/libmodA.so", "bin/libmodB.so"]


@needs_souffle
def test_a_required_edge_wins_over_an_optional_one(tmp_path, capsys):
    spec = (
        'runtime_dep = [["lib", "modA", "beside_loader"]]\n'
        'runtime_dep_optional = [["lib", "modA", "beside_loader"]]\n' + LIB
    )
    _, err, got = install(tmp_path, capsys, spec)
    assert err == "" and modules(got) == ["lib/libmodA.so"]


@needs_souffle
def test_conditions_gate_both_strengths(tmp_path, capsys):
    spec = (
        'runtime_dep_when = [["lib", "modA", "beside_loader", "never"]]\n'
        'runtime_dep_optional_when = [["lib", "modB", "beside_loader", "never"]]\n'
        'condition = [["never", "$Eq(native.opt, debug)"]]\n' + LIB
    )
    _, err, got = install(tmp_path, capsys, spec)
    assert err == "" and modules(got) == []


@needs_souffle
def test_unknown_place_and_condition_of_an_optional_module(tmp_path, capsys):
    _, err, _ = install(
        tmp_path, capsys, 'runtime_dep_optional = [["lib", "modA", "nearby"]]\n' + LIB
    )
    assert "unknown run-time dependency place" in err
    _, err, _ = install(
        tmp_path / "b",
        capsys,
        'runtime_dep_optional_when = [["lib", "modA", "beside_loader", "nope"]]\n' + LIB,
    )
    assert "unknown condition" in err


# ---- the Datalog export carries the requirement ----

from tests.test_runtime_dep import FILES as LOAD_FILES

REL = "arm64-macos.release.none"

# loader: a shared library that dlopens by name from the directory it is told;
# plug_a: a module returning 1. Exported as package Foo (component `loader`).
UP = """
target = [["loader"], ["plug_a"], ["plug_b"]]
native_kind = [["loader", "library"], ["plug_a", "module_library"], ["plug_b", "module_library"]]
default_linkage = [["loader", "shared"]]
native_version = [["loader", "1.0.0", "1"]]
c_source = [["loader", "loader.c"], ["plug_a", "plug.c"], ["plug_b", "plug.c"]]
prop = [["plug_a", "cc.define", "PLUG_VALUE=1", "private"], ["plug_b", "cc.define", "PLUG_VALUE=2", "private"]]
"""
EXPORT = """
request_all = [["loader"]]
export_package = [["Foo"]]
export_version = [["Foo", "1.0.0"]]
export_component = [["Foo", "loader", "loader"]]
"""
APP = """
target = [["app"]]
native_kind = [["app", "executable"]]
c_source = [["app", "main.c"]]
request = [["app", "rel"]]
"""
REQUIRED = 'runtime_dep = [["loader", "plug_a", "beside_executables"]]\n'


def upstream(tmp_path, spec, **kw):
    proj = project(tmp_path / "up", UP + EXPORT + spec, LOAD_FILES)
    (proj / "user.toml").write_text(f'install_prefix = "{tmp_path / "pfx"}"\n')
    build = tmp_path / "up" / "build"
    _, metrics = configure_project(
        proj, build, user_files=[proj / "user.toml"], update_lock=True, **kw
    )
    return build, metrics


def ninja(build, *args):
    return subprocess.run(
        ["ninja", "-C", str(build / "souffle"), *args], check=True, capture_output=True, text=True
    )


def facts_rows(prefix):
    text = (prefix / "share" / "buildlog" / "Foo" / "Foo.facts.dl").read_text()
    return {(r, v) for r, v, _ in dl_package.parse_facts(text)}


def downstream(tmp_path, prefix):
    """A program linking the imported loader, configured and built."""
    down = project(
        tmp_path / "down", APP + 'dep = [["app", "Foo:loader", "private"]]\n', LOAD_FILES
    )
    (down / "user.toml").write_text(
        f'package_search_dir = [["buildlog", "{prefix}/share/buildlog"]]\n'
    )
    build = tmp_path / "down" / "build"
    _, metrics = configure_project(down, build, user_files=[down / "user.toml"], update_lock=True)
    assert metrics["errors"] == []
    ninja(build)
    return build


def bin_layout(build):
    out = build / "souffle" / "bin" / REL
    return sorted(
        (p.name, p.is_symlink()) for p in out.iterdir() if "plug" in p.name or p.name == "app"
    )


@needs_souffle
def test_the_exported_requirement_is_placed_downstream_as_in_a_built_loader(tmp_path):
    build, metrics = upstream(tmp_path, REQUIRED)
    assert metrics["errors"] == []
    ninja(build, "install")
    prefix = tmp_path / "pfx"
    assert (prefix / "lib" / "libplug_a.so").is_file()
    rows = facts_rows(prefix)
    assert ("pkg_component", ("plug_a", "module")) in rows
    assert ("pkg_location", ("plug_a", "lib/libplug_a.so")) in rows
    assert ("pkg_runtime_dep", ("loader", "plug_a", "beside_executables")) in rows
    # A module is no default component: users of the package never link it.
    assert not any(r == "pkg_default" and v == ("plug_a",) for r, v in rows)

    imported = downstream(tmp_path, prefix)
    placed = imported / "souffle" / "bin" / REL / "libplug_a.so"
    assert placed.is_symlink() and placed.resolve() == (prefix / "lib" / "libplug_a.so").resolve()

    # The same program with the loader built in-tree places the module the same way.
    both = (
        UP.replace('[["loader"], ', '[["loader"], ["app"], ')
        .replace(
            '["plug_b", "module_library"]]', '["plug_b", "module_library"], ["app", "executable"]]'
        )
        .replace('["plug_b", "plug.c"]]', '["plug_b", "plug.c"], ["app", "main.c"]]')
    )
    built = project(
        tmp_path / "built",
        both + 'request = [["app", "rel"]]\ndep = [["app", "loader", "private"]]\n' + REQUIRED,
        LOAD_FILES,
    )
    configure_project(built, tmp_path / "built" / "build", update_lock=True)
    ninja(tmp_path / "built" / "build")
    assert bin_layout(imported) == bin_layout(tmp_path / "built" / "build")
    assert bin_layout(imported) == [("app", False), ("libplug_a.so", True)]
    # (An imported shared library has no rpath in the program: the loader's
    # directory is given by the environment.)
    env = {
        **os.environ,
        "DYLD_LIBRARY_PATH": str(prefix / "lib"),
        "LD_LIBRARY_PATH": str(prefix / "lib"),
    }
    for b in (imported, tmp_path / "built" / "build"):
        exe = str(b / "souffle" / "bin" / REL / "app")
        assert subprocess.check_output([exe, "libplug_a.so"], text=True, env=env) == "1\n"


@needs_souffle
def test_optional_modules_are_exported_only_when_installed_and_never_required(tmp_path, capsys):
    optional = 'runtime_dep_optional = [["loader", "plug_a", "beside_executables"], ["loader", "plug_b", "beside_executables"]]\n'
    capsys.readouterr()
    build, metrics = upstream(tmp_path, optional + 'install_target = [["plug_a", "libdir"]]\n')
    assert metrics["errors"] == []
    ninja(build, "install")
    prefix = tmp_path / "pfx"
    rows = facts_rows(prefix)
    assert ("pkg_component", ("plug_a", "module")) in rows
    assert ("pkg_location", ("plug_a", "lib/libplug_a.so")) in rows
    # Available, not required: no module for the unlisted one, no requirement.
    assert not any(v[0] == "plug_b" for _, v in rows if v)
    assert not any(r == "pkg_runtime_dep" for r, _ in rows)
    # Nothing is lost that a format could state: no warning.
    assert "run-time module" not in capsys.readouterr().err
    assert not (prefix / "lib" / "libplug_b.so").exists()
    # A consumer is given nothing: the module is available, not placed.
    imported = downstream(tmp_path, prefix)
    assert bin_layout(imported) == [("app", False)]


@needs_souffle
def test_required_placement_in_the_export_follows_the_install(tmp_path):
    spec = REQUIRED + 'install_target = [["app", "datadir"]]\n'
    up = UP + EXPORT + spec
    up = up.replace('target = [["loader"], ', 'target = [["loader"], ["app"], ', 1)
    up = up.replace(
        '["plug_b", "module_library"]]', '["plug_b", "module_library"], ["app", "executable"]]', 1
    )
    up = up.replace('["plug_b", "plug.c"]]', '["plug_b", "plug.c"], ["app", "main.c"]]', 1)
    up += 'dep = [["app", "loader", "private"]]\nrequest_all = [["app"]]\n'
    proj = project(tmp_path / "up", up.replace('request_all = [["loader"]]\n', ""), LOAD_FILES)
    (proj / "user.toml").write_text(f'install_prefix = "{tmp_path / "pfx"}"\n')
    build = tmp_path / "up" / "build"
    _, metrics = configure_project(proj, build, user_files=[proj / "user.toml"], update_lock=True)
    assert metrics["errors"] == []
    ninja(build, "install")
    rows = facts_rows(tmp_path / "pfx")
    # Beside the installed executable, which is what the program finds at run time.
    assert (tmp_path / "pfx" / "share" / "libplug_a.so").is_file()
    assert ("pkg_location", ("plug_a", "share/libplug_a.so")) in rows


@needs_souffle
def test_cmake_and_pkg_config_record_the_omission(tmp_path, capsys):
    capsys.readouterr()
    build, metrics = upstream(tmp_path, REQUIRED)
    err = capsys.readouterr().err
    assert metrics["errors"] == []
    for fmt in ("cmake", "cps", "pc"):
        assert f"Foo:loader needs plug_a at run time, which {fmt} cannot state" in err
    ninja(build, "install")
    prefix = tmp_path / "pfx"
    # Their files say nothing of the module, and the requirement is in the buildlog format.
    for f in prefix.rglob("*"):
        if f.is_file() and f.suffix in {".cmake", ".pc", ".cps"}:
            assert "plug_a" not in f.read_text(), f
    assert ("pkg_runtime_dep", ("loader", "plug_a", "beside_executables")) in facts_rows(prefix)

    # The author omits the formats that cannot carry it: the warnings go with them.
    omit = "".join(f'["Foo", "{f}", "no run-time modules"],' for f in ("cmake", "cps", "pc"))
    capsys.readouterr()
    build, metrics = upstream(
        tmp_path / "omitted", REQUIRED + f"export_format_omit = [{omit.rstrip(',')}]\n"
    )
    assert metrics["errors"] == []
    assert "run-time module" not in capsys.readouterr().err


TEMPLATES = """
runtime_dep = [["loadert", "{name}_req", "beside_loader"]]
runtime_dep_optional = [["loadert", "{name}_opt", "beside_loader"]]
install_target = [["one", "libdir"], ["two", "libdir"], ["one_opt", "bindir"]]
request_all = [["one"], ["two"]]

[target_template.loadert]
kind = "library"
default_linkage = "shared"
sources = ["lib.c"]

[target.one]
instance_of = "loadert"
[target.two]
instance_of = "loadert"

[target.one_req]
kind = "module_library"
sources = ["mod.c"]
[target.two_req]
kind = "module_library"
sources = ["mod.c"]
[target.one_opt]
kind = "module_library"
sources = ["mod.c"]
[target.two_opt]
kind = "module_library"
sources = ["mod.c"]
"""


@needs_souffle
def test_template_instances_expand_both_strengths(tmp_path, capsys):
    from tests.test_cpp_mechanisms import project as make

    proj = make(tmp_path, TEMPLATES, FILES)
    (proj / "user.toml").write_text(f'install_prefix = "{tmp_path / "pfx"}"\n')
    capsys.readouterr()
    configure_project(
        proj, tmp_path / "build", user_files=[proj / "user.toml"], update_lock=True, emit=False
    )
    path = tmp_path / "build" / "souffle" / "facts" / "installed.csv"
    got = sorted(r.split("\t")[2] for r in path.read_text().splitlines())
    # Each instance installs its own required module; an optional one only if listed.
    assert [n for n in got if "_req" in n or "_opt" in n] == [
        "bin/libone_opt.so",
        "lib/libone_req.so",
        "lib/libtwo_req.so",
    ]
