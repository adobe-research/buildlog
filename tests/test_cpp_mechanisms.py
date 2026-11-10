# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generic buildlog-cpp mechanisms added for the Halide/LLVM case study:
the strict `<Pkg>Config.cmake` literal reader (a helper library for
discovery plugins), system include
dirs (-isystem), the C++ ABI (RTTI/exceptions) compatibility relation,
shared libraries, C++ sources an action generates, and module and object libraries, whole archives of built targets,
interface dependencies and build-tree rpaths."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from buildlog_cpp.importers import cmake_config
from buildlog_cpp.importers.common import MetadataError

from buildlog.engine import configure_project
from buildlog.facts import read_rows
from buildlog.paths import souffle_path

from . import host

ROOT = Path(__file__).resolve().parents[1]
GREET_CMAKE = ROOT / "tests/fixtures/importers/prefix/lib/cmake/Greet/Greet-targets.cmake"

needs_souffle = pytest.mark.skipif(
    not souffle_path().exists(), reason="patched Souffle not installed"
)


@pytest.fixture(autouse=True)
def _no_ambient_package_path(monkeypatch):
    monkeypatch.delenv("BUILDLOG_PACKAGE_PATH", raising=False)


# ---- the literal reader ------------------------------------------------------

CONFIG = """\
# A configured package config, in the shape of LLVMConfig.cmake.
get_filename_component(PKG_INSTALL_PREFIX "${CMAKE_CURRENT_LIST_FILE}" REALPATH)
get_filename_component(PKG_INSTALL_PREFIX "${PKG_INSTALL_PREFIX}" PATH)
get_filename_component(PKG_INSTALL_PREFIX "${PKG_INSTALL_PREFIX}" PATH)
set(PKG_VERSION 22.1.8)
set(PKG_TARGETS "AArch64;WebAssembly;X86")
set(PKG_ENABLE_RTTI OFF)
set(PKG_INCLUDE_DIRS "${PKG_INSTALL_PREFIX}/include")
set(PKG_DEFINITIONS "-D_GNU_SOURCE -D__STDC_LIMIT_MACROS -DX=1")
if(PKG_ENABLE_RTTI)
  message(STATUS "rtti")
endif()
foreach(t ${PKG_TARGETS})
endforeach()
"""


def write_config(tmp_path, text=CONFIG, name="PkgConfig.cmake"):
    d = tmp_path / "prefix" / "lib" / "cmake" / "pkg"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(text)
    return f


def value(path, var, form):
    items, line = cmake_config.read_value(path, var)
    return cmake_config.typed(items, line, form)


def test_reader_forms_and_lines(tmp_path):
    f = write_config(tmp_path)
    prefix = os.path.realpath(tmp_path / "prefix" / "lib" / "cmake")
    assert value(f, "PKG_VERSION", "version")[0] == [
        ("value", "22.1.8", 5),
        ("major", "22", 5),
        ("minor", "1", 5),
        ("patch", "8", 5),
    ]
    assert value(f, "PKG_TARGETS", "list")[1] == [
        (0, "item", "AArch64", 6),
        (1, "item", "WebAssembly", 6),
        (2, "item", "X86", 6),
    ]
    assert value(f, "PKG_ENABLE_RTTI", "bool")[0] == [("value", "off", 7)]
    # The relocation idiom is evaluated: the realpath of the file, N up.
    assert value(f, "PKG_INSTALL_PREFIX", "path")[0] == [("value", prefix, 4)]
    assert value(f, "PKG_INCLUDE_DIRS", "paths")[1] == [(0, "path", prefix + "/include", 8)]
    assert value(f, "PKG_DEFINITIONS", "compile_flags")[1] == [
        (0, "define", "_GNU_SOURCE", 9),
        (1, "define", "__STDC_LIMIT_MACROS", 9),
        (2, "define", "X=1", 9),
    ]


@pytest.mark.parametrize(
    "extra, var, message",
    [
        ("set(PKG_VERSION 23)\n", "PKG_VERSION", "set more than once (lines 5, 15)"),
        ("if(A)\n  set(PKG_X 1)\nendif()\n", "PKG_X", "set conditionally"),
        ("list(APPEND PKG_TARGETS Y)\n", "PKG_TARGETS", "modified by list()"),
        ("string(TOUPPER x PKG_VERSION)\n", "PKG_VERSION", "modified by string()"),
        ("set(PKG_Y ${OTHER}/x)\n", "PKG_Y", "variable reference"),
        ("set(PKG_Y $<CONFIG>)\n", "PKG_Y", "generator expression"),
        ("set(PKG_Y 1 CACHE STRING x)\n", "PKG_Y", "not a literal value"),
        ("", "PKG_NOPE", "PKG_NOPE is not set"),
        ("function(f)\n  set(PKG_Z 1)\nendfunction()\n", "PKG_Z", "set conditionally"),
    ],
)
def test_reader_rejects_what_is_not_literal(tmp_path, extra, var, message):
    f = write_config(tmp_path, CONFIG + extra)
    with pytest.raises(MetadataError, match=message.replace("(", r"\(").replace(")", r"\)")):
        cmake_config.read_value(f, var)


@pytest.mark.parametrize(
    "text, form, message",
    [
        ("set(V maybe)\n", "bool", "not a CMake boolean"),
        ("set(V 22.x)\n", "version", "not a version"),
        ("set(V rel/path)\n", "paths", "not an absolute path"),
        ('set(V "-DA -fno-rtti")\n', "compile_flags", "not a -D or -I flag"),
        ('set(V "/a;/b")\n', "path", "expected one path"),
    ],
)
def test_reader_rejects_ill_typed_values(tmp_path, text, form, message):
    f = write_config(tmp_path, text)
    with pytest.raises(MetadataError, match=message):
        value(f, "V", form)


def test_reader_library_origins_and_errors(tmp_path):
    f = write_config(tmp_path)
    v = cmake_config.value(f, "PKG_DEFINITIONS", "compile_flags")
    assert (2, "define", "X=1", f"{f}:9") in v.items
    # The value's own origin too: an empty or absent item list still has its set() line.
    assert v.origin == f"{f}:9"
    ver = cmake_config.value(f, "PKG_VERSION", "version")
    assert ver.field("major") == "22" and ("minor", "1", f"{f}:5") in ver.fields
    with pytest.raises(MetadataError) as exc:
        cmake_config.value(f, "PKG_TARGETS", "bool")
    msg, where = cmake_config.error_text(f, "PKG_TARGETS", exc.value)
    assert msg.startswith(f"{f}:6: PKG_TARGETS: not a CMake") and where == f"{f}:6"


def test_config_find_lists_every_candidate(tmp_path):
    a = write_config(tmp_path, "set(PACKAGE_VERSION 1.0)\n")
    (a.parent / "PkgConfigVersion.cmake").write_text("set(PACKAGE_VERSION 1.0)\n")
    b = tmp_path / "prefix" / "pkg-2" / "share" / "cmake" / "pkg"
    b.mkdir(parents=True)
    (b / "pkg-config.cmake").write_text("")
    found = cmake_config.find_configs("Pkg", tmp_path / "prefix")
    assert found == sorted(
        [
            (os.path.realpath(a), os.path.realpath(a.parent / "PkgConfigVersion.cmake")),
            (os.path.realpath(b / "pkg-config.cmake"), ""),
        ]
    )


# ---- end to end ---------------------------------------------------------------


def cc_check_fields(req: str) -> list[str]:
    """A cc_check request id's fields (toolchain, exec, target, lang, kind,
    subject, headers, args): each but the last is length-prefixed."""
    rest, out = req.removeprefix("cc-check:"), []
    for _ in range(7):
        n, _, rest = rest.partition(":")
        out.append(rest[: int(n)])
        rest = rest[int(n) + 1 :]
    return out + [rest]


def project(tmp_path, spec, files=None, packages=None):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    for name, text in (files or {}).items():
        (proj / "src" / name).write_text(text)
    for name, (manifest, dl) in (packages or {}).items():
        d = proj / "packages" / name
        d.mkdir(parents=True)
        (d / "manifest.toml").write_text(manifest)
        (d / f"{name}.dl").write_text(dl)
    uses = ["buildlog-cpp", *(packages or {})]
    (proj / "buildlog.toml").write_text(
        f'uses = {json.dumps(uses)}\nsource_root = {json.dumps(str(proj / "src"))}\nspec = ["spec.toml"]\n'
        "[author_decides]\n"
        'toolchain_pin = "test fixture pins Apple clang"\n'
        'package_file = "test fixture binds package metadata"\n'
        'package_system_includes = "test fixture checks include spelling"\n'
    )
    (proj / "spec.toml").write_text(
        """
config_decl = [["rel", "arm64-macos"]]
config_decl_setting = [["rel", "native.opt", "release"]]
toolchain_pin = [["c", "arm64-macos", "arm64-macos", "apple-clang"], ["cxx", "arm64-macos", "arm64-macos", "apple-clang"], ["native-link", "arm64-macos", "arm64-macos", "apple-clang"]]
"""
        + spec
    )
    return proj


def configure(proj, tmp_path):
    build = tmp_path / "build"
    _, metrics = configure_project(proj, build, update_lock=True)
    return build, metrics


def configure_fails(proj, tmp_path, capsys):
    with pytest.raises(SystemExit):
        configure_project(proj, tmp_path / "build", emit=False, update_lock=True)
    return capsys.readouterr().err


def ninja_text(build):
    return (build / "souffle" / "build.ninja").read_text()


def cmd_of(build, output_part):
    """The cmd of the build statement whose outputs contain output_part."""
    lines = ninja_text(build).splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("build ") and output_part in ln.split(":")[0]:
            return next(x for x in lines[i + 1 :] if x.startswith("  cmd = "))
    raise AssertionError(output_part)


def run_ninja(build):
    subprocess.run(["ninja", "-C", str(build / "souffle")], check=True, capture_output=True)


# ---- system include dirs -------------------------------------------------------


@needs_souffle
def test_imported_include_dirs_are_isystem_with_or_without_package_system_includes(tmp_path):
    main_cpp = '#include "greet.h"\nint main() { return 0; }\n'
    base = f"""
package_file = [["cmake", {json.dumps(str(GREET_CMAKE))}]]
target = [["hello"]]
native_kind = [["hello", "executable"]]
cxx_source = [["hello", "main.cpp"]]
dep = [["hello", "Greet::shout", "private"]]
prop = [["hello", "cc.include_dir", "src:.", "private"]]
request = [["hello", "rel"]]
"""
    proj = project(tmp_path, base, {"main.cpp": main_cpp})
    build, _ = configure(proj, tmp_path)
    inc = str(GREET_CMAKE.parents[3] / "include")
    # A dependency's include dirs are -isystem, imported or not (the
    # dependency class; tests/test_include_order.py has the class order).
    line = next(ln for ln in ninja_text(build).splitlines() if f"-isystem {inc}" in ln)
    assert f"-I{inc}" not in line and line.index("-I") < line.index("-isystem")
    (proj / "spec.toml").write_text(
        (proj / "spec.toml").read_text() + 'package_system_includes = [["cmake"]]\n'
    )
    build, _ = configure(proj, tmp_path)
    line = next(ln for ln in ninja_text(build).splitlines() if f"-isystem {inc}" in ln)
    # -I dirs first, then -isystem ones (as the compiler searches them).
    assert f"-I{inc}" not in line and line.index("-I") < line.index("-isystem")


# ---- C++ ABI: RTTI and exceptions ----------------------------------------------


def abi_project(tmp_path, app_props, lib_props):
    props = [["app", k, v, "private"] for k, v in app_props] + [
        ["lib", k, v, "private"] for k, v in lib_props
    ]
    return project(
        tmp_path,
        f"""
target = [["app"], ["lib"]]
native_kind = [["app", "executable"], ["lib", "library"]]
cxx_source = [["app", "main.cpp"], ["lib", "lib.cpp"]]
dep = [["app", "lib", "private"]]
prop = {json.dumps(props)}
request = [["app", "rel"]]
""",
        {
            "main.cpp": "int f();\nint main() { return f(); }\n",
            "lib.cpp": "int f() { return 0; }\n",
        },
    )


@needs_souffle
def test_abi_matching_settings_build_and_spell_their_flags(tmp_path):
    off = [("cxx.rtti", "off"), ("cxx.exceptions", "off")]
    proj = abi_project(tmp_path, [("cxx.rtti", "off")], off)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    lib_cmd = cmd_of(build, "lib.cpp.o")
    app_cmd = cmd_of(build, "main.cpp.o")
    assert "-fno-rtti" in lib_cmd and "-fno-exceptions" in lib_cmd
    # Exceptions are directional: an app with exceptions may use lib without.
    assert "-fno-rtti" in app_cmd and "-fno-exceptions" not in app_cmd
    run_ninja(build)


@needs_souffle
def test_abi_rtti_mismatch_is_an_error(tmp_path, capsys):
    err = configure_fails(abi_project(tmp_path, [], [("cxx.rtti", "off")]), tmp_path, capsys)
    assert "C++ RTTI mismatch" in err
    assert "app is built with cxx.rtti=on but uses lib, built with cxx.rtti=off" in err


@needs_souffle
def test_abi_exceptions_off_cannot_use_a_library_with_exceptions(tmp_path, capsys):
    err = configure_fails(abi_project(tmp_path, [("cxx.exceptions", "off")], []), tmp_path, capsys)
    assert "C++ exceptions mismatch" in err
    assert "app is built with cxx.exceptions=off but uses lib" in err


@needs_souffle
def test_abi_imported_library_states_its_settings(tmp_path, capsys):
    # An interface/imported library states how it was built; a consumer that
    # disagrees is an error, and the setting does not reach its flags.
    proj = project(
        tmp_path,
        """
target = [["app"], ["ext"]]
native_kind = [["app", "executable"]]
cxx_source = [["app", "main.cpp"]]
dep = [["app", "ext", "private"]]
prop = [["ext", "cxx.rtti", "off", "private"]]
request = [["app", "rel"]]
""",
        {"main.cpp": "int main() { return 0; }\n"},
    )
    err = configure_fails(proj, tmp_path, capsys)
    assert "app is built with cxx.rtti=on but uses ext, built with cxx.rtti=off" in err


@needs_souffle
def test_abi_invalid_value_is_an_error(tmp_path, capsys):
    err = configure_fails(abi_project(tmp_path, [("cxx.rtti", "maybe")], []), tmp_path, capsys)
    assert "invalid C++ ABI setting" in err


# ---- shared libraries ----------------------------------------------------------


@needs_souffle
@host.macos_only("Mach-O spellings: exported-symbols list, compatibility versions (test_elf.py)")
def test_shared_library_links_versioned_and_runs(tmp_path):
    proj = project(
        tmp_path,
        """
target = [["app"], ["answer"]]
native_kind = [["app", "executable"], ["answer", "library"]]
default_linkage = [["answer", "shared"]]
cxx_source = [["app", "main.cpp"], ["answer", "answer.cpp"]]
native_version = [["answer", "3.1.0", "3"]]
native_exported_symbols = [["answer", "src:exports.txt"]]
prop = [["answer", "native.link_option", "-Wl,-headerpad_max_install_names", "private"]]
dep = [["app", "answer", "private"]]
request = [["app", "rel"]]
""",
        {
            "main.cpp": '#include <cstdio>\nint answer();\nint main() { std::printf("%d\\n", answer()); }\n',
            "answer.cpp": "int answer() { return 42; }\nint hidden() { return 1; }\n",
            "exports.txt": "__Z6answerv\n",
        },
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    text = ninja_text(build)
    link = next(ln for ln in text.splitlines() if "-dynamiclib" in ln)
    # The build-tree rpath is core's: app links a built shared library.
    assert cmd_of(build, "bin/").endswith(
        "libanswer.3.1.0.dylib -Wl,-rpath,@loader_path/../../lib/arm64-macos.release.none"
    )
    for piece in (
        "-Wl,-headerpad_max_install_names",
        "-Xlinker -exported_symbols_list -Xlinker",
        "-compatibility_version 3.0.0",
        "-current_version 3.1.0",
        "-install_name @rpath/libanswer.3.dylib",
    ):
        assert piece in link
    assert " -fPIC " in cmd_of(build, "answer.cpp.o")
    assert " -fPIC " not in cmd_of(build, "main.cpp.o")
    # The development link alone brings the install-name link it points at.
    rel = "arm64-macos.release.none"
    subprocess.run(
        ["ninja", "-C", str(build / "souffle"), f"lib/{rel}/libanswer.dylib"],
        check=True, capture_output=True,
    )  # fmt: skip
    assert (build / "souffle" / "lib" / rel / "libanswer.dylib").resolve().exists()
    run_ninja(build)
    libdir = build / "souffle" / "lib"
    names = {p.name for p in libdir.rglob("libanswer*")}
    assert names == {"libanswer.3.1.0.dylib", "libanswer.3.dylib", "libanswer.dylib"}
    exe = next((build / "souffle" / "bin").glob("*/app"))
    assert subprocess.check_output([str(exe)], text=True) == "42\n"
    syms = subprocess.check_output(
        ["nm", "-gU", str(next(libdir.rglob("libanswer.3.1.0.dylib")))], text=True
    )
    assert "__Z6answerv" in syms and "hidden" not in syms


_VERSION_SCRIPT_SPEC = """
target = [["answer"]]
native_kind = [["answer", "library"]]
default_linkage = [["answer", "shared"]]
cxx_source = [["answer", "answer.cpp"]]
native_version_script = [["answer", "src:exports.ld"]]
%s
request = [["answer", "rel"]]
"""
_VERSION_SCRIPT_FILES = {
    "answer.cpp": 'extern "C" int shown() { return 1; }\n',
    "exports.ld": "{ global: shown; local: *; };\n",
}


@needs_souffle
@host.macos_only("a version script has no Mach-O spelling (test_elf.py has the ELF link)")
def test_a_version_script_that_applies_nowhere_is_an_error(tmp_path, capsys):
    proj = project(tmp_path, _VERSION_SCRIPT_SPEC % "", _VERSION_SCRIPT_FILES)
    assert "export list but no spelling for it" in configure_fails(proj, tmp_path, capsys)


@needs_souffle
@host.macos_only("a version script has no Mach-O spelling (test_elf.py has the ELF link)")
def test_an_optional_version_script_is_left_out_where_it_has_no_spelling(tmp_path):
    spec = _VERSION_SCRIPT_SPEC % 'native_export_list_optional = [["answer", "ELF only"]]'
    proj = project(tmp_path, spec, _VERSION_SCRIPT_FILES)
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    link = cmd_of(build, "libanswer")
    assert "version-script" not in link and "exports.ld" not in link
    run_ninja(build)


@needs_souffle
def test_version_on_a_target_that_is_not_a_library_is_an_error(tmp_path, capsys):
    # A library's version applies to its shared build (a static build has
    # none); on any other kind it is an error.
    proj = project(
        tmp_path,
        """
target = [["answer"]]
native_kind = [["answer", "module_library"]]
cxx_source = [["answer", "answer.cpp"]]
native_version = [["answer", "1.0", "1"]]
request = [["answer", "rel"]]
""",
        {"answer.cpp": "int answer() { return 42; }\n"},
    )
    assert "native_version of answer" in configure_fails(proj, tmp_path, capsys)


# ---- generated C++ sources -------------------------------------------

EMBED_MANIFEST = """\
name = "embedgen"
version = "0.1.0"
rank = 5
namespace = "embedgen"
layers = ["embedgen.dl"]
requires = { buildlog-cpp = ">=0.1" }
extends = ["action", "action_run", "action_stdin", "action_stdout", "target_action",
           "cxx_generated_source"]
accepts = ["embedgen_source"]
"""
# embedgen_source(t, tool, input): `tool < input > out:gen/<config>/<t>.cpp`,
# compiled into t. The tool is named by reference; nothing says where it runs.
EMBED_DL = """\
.decl embedgen_source(t:symbol, tool:symbol, in:symbol)
.decl embedgen_out(t:symbol, c:Cfg, o:symbol, g:symbol, i:symbol)
embedgen_out(t, c, cat("out:gen/", n, "/", t, ".cpp"), g, i) :- embedgen_source(t, g, i), needed(t, c),
  cfg_name(c, n).
action(o, "EMBED"), target_action(t, c, o), action_stdout(o, o), cxx_generated_source(t, c, o) :-
  embedgen_out(t, c, o, _, _).
action_run(o, 0, "", g, "run") :- embedgen_out(_, _, o, g, _).
action_stdin(o, cat("src:", i)) :- embedgen_out(_, _, o, _, i).
"""


@needs_souffle
def test_generated_source_from_a_tool_in_the_graph_compiles_and_links(tmp_path):
    proj = project(
        tmp_path,
        """
target = [["app"], ["emit"]]
native_kind = [["app", "executable"], ["emit", "executable"]]
cxx_source = [["app", "main.cpp"], ["emit", "emit.cpp"]]
embedgen_source = [["app", "emit", "value.txt"]]
request = [["app", "rel"]]
""",
        {
            "main.cpp": '#include <cstdio>\nint value();\nint main() { std::printf("%d\\n", value()); }\n',
            "emit.cpp": '#include <cstdio>\nint main() { int v = 0; std::scanf("%d", &v);\n'
            '  std::printf("int value() { return %d; }\\n", v); }\n',
            "value.txt": "41\n",
        },
        packages={"embedgen": (EMBED_MANIFEST, EMBED_DL)},
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    facts = build / "souffle" / "facts"
    cfg = "[arm64-macos, [[native.opt, release], [[native.sanitize, none], [[native.linkage, default], nil]]]]"
    assert (facts / "tool_dep.csv").read_text() == f"app\t{cfg}\temit\n"
    rel = "arm64-macos.release.none"
    assert cmd_of(build, f"gen/{rel}/app.cpp").endswith(
        f"cmd = bin/{rel}/emit < ../../proj/src/value.txt > gen/{rel}/app.cpp"
    )
    # The generated source compiles like app's own, into obj/<config>/app/<path>.o.
    assert f" -c gen/{rel}/app.cpp" in cmd_of(build, f"obj/{rel}/app/gen/{rel}/app.cpp.o")
    run_ninja(build)
    exe = build / "souffle" / "bin" / rel / "app"
    assert subprocess.check_output([str(exe)], text=True) == "41\n"


# ---- linking: plugins, whole archives, object libraries ----------


@needs_souffle
def test_modules_whole_archives_object_libraries_and_rpaths(tmp_path):
    """One app exercises core's phase-4 link mechanisms: a module library
    (dlopen'ed), a spec-stated whole-archive edge of a built archive, an
    object library linked into two targets, an interface dependency, a shared
    library's hidden private dependency, and build-tree rpaths."""
    proj = project(
        tmp_path,
        """
target = [["app"], ["core"], ["secret"], ["api"], ["gen"], ["plugin_api"], ["objs"], ["mod"]]
native_kind = [["app", "executable"], ["core", "library"], ["secret", "library"],
  ["gen", "library"], ["objs", "object_library"], ["mod", "module_library"]]
default_linkage = [["core", "shared"]]
cxx_source = [["app", "main.cpp"], ["core", "core.cpp"], ["secret", "secret.cpp"],
  ["gen", "gen.cpp"], ["objs", "obj.cpp"], ["mod", "mod.cpp"]]
prop = [["api", "cc.include_dir", "src:api", "interface"]]
dep = [["core", "secret", "private"], ["core", "api", "interface"],
  ["plugin_api", "gen", "public"],
  ["app", "core", "private"], ["app", "plugin_api", "private"], ["app", "objs", "private"],
  ["mod", "core", "private"], ["mod", "objs", "private"]]
whole_archive = [["plugin_api", "gen"]]
request = [["app", "rel"], ["mod", "rel"]]
""",
        {
            "secret.cpp": "int secret() { return 1; }\n",
            "core.cpp": "int secret();\nint core() { return secret() + 1; }\n",
            "gen.cpp": '#include <cstdio>\nstruct R { R() { std::puts("registered"); } } r;\n',
            "obj.cpp": "int obj() { return 4; }\n",
            "mod.cpp": 'int obj(); int core();\nextern "C" int mod_value() { return obj() * 10 + core(); }\n',
            "main.cpp": '#include <cstdio>\n#include <dlfcn.h>\n#include "api.h"\n'
            "int core(); int obj();\nint main(int, char** argv) {\n"
            "  void* h = dlopen(argv[1], RTLD_NOW);\n  if (!h) { std::puts(dlerror()); return 1; }\n"
            '  auto f = (int (*)())dlsym(h, "mod_value");\n'
            '  std::printf("%d %d %d %d\\n", core(), obj(), API_VALUE, f());\n}\n',
        },
    )
    (proj / "src" / "api").mkdir()
    (proj / "src" / "api" / "api.h").write_text("#define API_VALUE 3\n")
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    rel = "arm64-macos.release.none"
    rpath = host.rpath(f"../../lib/{rel}")
    app = cmd_of(build, f"bin/{rel}/app")
    # The whole archive once, forced; the object library's object; core's
    # private static dependency hidden behind it; the rpath to core.
    assert app.count("libgen.a") == 1 and host.force_load(f"lib/{rel}/libgen.a") in app
    assert f"obj/{rel}/objs/obj.cpp.o" in app and "libsecret.a" not in app
    assert app.endswith(rpath)
    assert "libsecret.a" in cmd_of(build, f"lib/{rel}/{host.shlib('core')}")
    # The interface dependency reaches core's consumers only.
    assert "-isystem ../../proj/src/api" in cmd_of(build, f"obj/{rel}/app/main.cpp.o")
    assert "src/api" not in cmd_of(build, f"obj/{rel}/core/core.cpp.o")
    mod = cmd_of(build, f"lib/{rel}/libmod.so")
    assert f" {host.MODULE_FLAG} " in mod and f"obj/{rel}/objs/obj.cpp.o" in mod
    assert mod.endswith(rpath)
    assert " -fPIC " in cmd_of(build, f"obj/{rel}/mod/mod.cpp.o")
    assert ninja_text(build).count("obj.cpp.o:") == 1  # compiled once, for both
    run_ninja(build)
    out = build / "souffle"
    got = subprocess.check_output(
        [str(out / "bin" / rel / "app"), str(out / "lib" / rel / "libmod.so")], text=True
    )
    assert got == "registered\n2 4 3 42\n"


@needs_souffle
def test_unknown_dependency_visibility_is_an_error(tmp_path, capsys):
    proj = project(
        tmp_path,
        """
target = [["app"], ["lib"]]
native_kind = [["app", "executable"], ["lib", "library"]]
cxx_source = [["app", "main.cpp"], ["lib", "lib.cpp"]]
dep = [["app", "lib", "protected"]]
request = [["app", "rel"]]
""",
        {"main.cpp": "int main() { return 0; }\n", "lib.cpp": "int f() { return 0; }\n"},
    )
    assert "unknown visibility" in configure_fails(proj, tmp_path, capsys)


# ---- C sources, C++-only flags, PIC, reference lists -----------

LOADER_MANIFEST = """\
name = "loadgen"
version = "0.1.0"
rank = 5
namespace = "loadgen"
layers = ["loadgen.dl"]
requires = { buildlog-cpp = ">=0.1" }
extends = ["action", "action_run", "action_run_list", "action_run_item", "action_stdout",
           "target_action", "cxx_generated_source"]
accepts = ["loadgen_source", "loadgen_plugin"]
"""
# loadgen_source(t, tool): `tool -p <plugin>,<plugin>... > out:gen/<config>/<t>.cpp`,
# compiled into t; the plugins, in order, are loadgen_plugin(t, i, plugin).
LOADER_DL = """\
.decl loadgen_source(t:symbol, tool:symbol)
.decl loadgen_plugin(t:symbol, i:number, plugin:symbol)
.decl loadgen_out(t:symbol, c:Cfg, o:symbol, g:symbol)
loadgen_out(t, c, cat("out:gen/", n, "/", t, ".cpp"), g) :- loadgen_source(t, g), needed(t, c),
  cfg_name(c, n).
action(o, "LOADGEN"), target_action(t, c, o), action_stdout(o, o), cxx_generated_source(t, c, o),
action_run_list(o, 1, "-p ", ",") :- loadgen_out(t, c, o, _).
action_run(o, 0, "", g, "run") :- loadgen_out(_, _, o, g).
action_run_item(o, 1, i, p, "load") :- loadgen_out(t, _, o, _), loadgen_plugin(t, i, p).
"""


@needs_souffle
def test_c_sources_cxx_only_flags_pic_and_reference_lists(tmp_path):
    """A C source compiles with the C compiler and C-family flags only; a
    cxx.compile_flag reaches C++ compiles only; cc.pic makes a static
    library's objects position-independent; a tool loads two plugins named
    as one comma-separated list of references, so both are built for exec
    and spelled, in order, in one argv word."""
    proj = project(
        tmp_path,
        """
target = [["app"], ["flags"], ["loader"], ["pa"], ["pb"], ["pic"]]
native_kind = [["app", "executable"], ["loader", "executable"], ["pa", "module_library"],
  ["pb", "module_library"], ["pic", "library"]]
cxx_source = [["app", "main.cpp"], ["loader", "loader.cpp"], ["pa", "pa.cpp"], ["pb", "pb.cpp"],
  ["pic", "pic.cpp"]]
c_source = [["app", "twice.c"]]
prop = [["flags", "cc.compile_flag", "-Wall", "interface"],
  ["flags", "cxx.compile_flag", "-Woverloaded-virtual", "interface"],
  ["flags", "cc.define", "FLAVOR=2", "interface"],
  ["pic", "cc.pic", "on", "private"]]
dep = [["app", "flags", "private"], ["app", "pic", "private"]]
loadgen_source = [["app", "loader"]]
loadgen_plugin = [["app", 0, "pb"], ["app", 1, "pa"]]
request = [["app", "rel"]]
""",
        {
            "twice.c": "int twice(int x) { return FLAVOR * x; }\n",
            "pic.cpp": "int pic() { return 1; }\n",
            "pa.cpp": 'extern "C" int value() { return 20; }\n',
            "pb.cpp": 'extern "C" int value() { return 1; }\n',
            "loader.cpp": "#include <cstdio>\n#include <cstring>\n#include <dlfcn.h>\n#include <string>\n"
            "int main(int argc, char** argv) {\n  int sum = 0;\n"
            "  std::string list = argv[2];\n  size_t at = 0;\n"
            "  while (at <= list.size()) {\n    size_t end = list.find(',', at);\n"
            "    if (end == std::string::npos) end = list.size();\n"
            "    void* h = dlopen(list.substr(at, end - at).c_str(), RTLD_NOW | RTLD_LOCAL);\n"
            "    if (!h) return 1;\n"
            '    sum = sum * 10 + ((int (*)())dlsym(h, "value"))();\n    at = end + 1;\n  }\n'
            '  std::printf("int loaded() { return %d; }\\n", sum);\n}\n',
            "main.cpp": '#include <cstdio>\nextern "C" int twice(int);\nint loaded(); int pic();\n'
            'int main() { std::printf("%d %d %d\\n", twice(21), loaded(), pic()); }\n',
        },
        packages={"loadgen": (LOADER_MANIFEST, LOADER_DL)},
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    rel = "arm64-macos.release.none"
    c_cmd = cmd_of(build, f"obj/{rel}/app/twice.c.o")
    assert c_cmd.split()[2].endswith(f"/{host.CC}")
    assert "-Wall" in c_cmd and "-DFLAVOR=2" in c_cmd
    assert "-std=" not in c_cmd and "-Woverloaded-virtual" not in c_cmd
    cxx_cmd = cmd_of(build, f"obj/{rel}/app/main.cpp.o")
    assert cxx_cmd.split()[2].endswith(f"/{host.CXX}")
    assert "-Wall" in cxx_cmd and "-Woverloaded-virtual" in cxx_cmd
    assert " -fPIC " in cmd_of(build, f"obj/{rel}/pic/pic.cpp.o")
    assert " -fPIC " not in cxx_cmd
    gen = cmd_of(build, f"gen/{rel}/app.cpp")
    assert f"-p lib/{rel}/libpb.so,lib/{rel}/libpa.so > gen/{rel}/app.cpp" in gen
    facts = build / "souffle" / "facts"
    assert sorted(
        ln.split("\t")[2] for ln in (facts / "tool_dep.csv").read_text().splitlines()
    ) == [
        "loader",
        "pa",
        "pb",
    ]
    run_ninja(build)
    exe = build / "souffle" / "bin" / rel / "app"
    assert subprocess.check_output([str(exe)], text=True) == "42 30 1\n"  # pb (1), then pa (20)


# ---- link options propagate like INTERFACE_LINK_OPTIONS ----------


# Three linker options the host's linker takes: ld64's, or GNU ld's.
LINK_OPTS = (
    ("-Wl,-dead_strip", "-Wl,-headerpad_max_install_names", "-Wl,-no_uuid")
    if host.MACOS
    else ("-Wl,--gc-sections", "-Wl,-z,now", "-Wl,--build-id=none")
)


@needs_souffle
def test_link_options_reach_consumers_by_visibility(tmp_path):
    """A native.link_option is a linked artifact's own when private; public and
    interface ones also reach its consumers' links, transitively (CMake's
    INTERFACE_LINK_OPTIONS), through an interface target too."""
    proj = project(
        tmp_path,
        """
target = [["app"], ["opts"], ["lib"]]
native_kind = [["app", "executable"], ["lib", "library"]]
cxx_source = [["app", "main.cpp"], ["lib", "lib.cpp"]]
prop = [["opts", "native.link_option", "{}", "interface"],
  ["lib", "native.link_option", "{}", "public"],
  ["lib", "native.link_option", "{}", "private"]]
dep = [["app", "lib", "private"], ["lib", "opts", "public"]]
request = [["app", "rel"]]
""".format(*LINK_OPTS),
        {
            "main.cpp": "int f();\nint main() { return f(); }\n",
            "lib.cpp": "int f() { return 0; }\n",
        },
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    link = cmd_of(build, "bin/arm64-macos.release.none/app")
    assert LINK_OPTS[0] in link and LINK_OPTS[1] in link
    assert LINK_OPTS[2] not in link
    run_ninja(build)


# ---- output names, the C link driver, Mach-O versions (zlib case study) --------


@needs_souffle
@host.macos_only("Mach-O compatibility and current versions (test_elf.py covers ELF names)")
def test_output_names_c_link_driver_and_padded_versions(tmp_path):
    """Two variants of one library share an output name (CMake's OUTPUT_NAME):
    libz.a and libz.1.3.2.dylib. A C-only program links with the C driver
    (no C++ runtime); one with a C++ object keeps the C++ driver. The
    compatibility version is the soversion padded to X.Y.Z, the current
    version the full version."""
    proj = project(
        tmp_path,
        """
target = [["zshared"], ["zstatic"], ["cprog"], ["cxxprog"]]
native_kind = [["zshared", "library"], ["zstatic", "library"],
  ["cprog", "executable"], ["cxxprog", "executable"]]
default_linkage = [["zshared", "shared"]]
native_output_name = [["zshared", "z"], ["zstatic", "z"]]
native_version = [["zshared", "1.3", "1"]]
c_source = [["zshared", "z.c"], ["zstatic", "z.c"], ["cprog", "main.c"]]
cxx_source = [["cxxprog", "main.cpp"]]
dep = [["cprog", "zshared", "private"], ["cxxprog", "zstatic", "private"]]
request = [["cprog", "rel"], ["cxxprog", "rel"]]
""",
        {
            "z.c": "int z(void) { return 42; }\n",
            "main.c": '#include <stdio.h>\nint z(void);\nint main(void) { printf("%d\\n", z()); return 0; }\n',
            "main.cpp": '#include <cstdio>\nextern "C" int z();\nint main() { std::printf("%d\\n", z()); }\n',
        },
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    rel = "arm64-macos.release.none"
    link = cmd_of(build, f"lib/{rel}/libz.1.3.dylib")
    assert "-compatibility_version 1.0.0 -current_version 1.3.0" in link
    assert "-install_name @rpath/libz.1.dylib" in link
    assert link.split()[2].endswith("/clang")
    assert cmd_of(build, f"lib/{rel}/libz.a").split()[4] == f"lib/{rel}/libz.a"
    assert cmd_of(build, f"bin/{rel}/cprog").split()[2].endswith("/clang")
    assert cmd_of(build, f"bin/{rel}/cxxprog").split()[2].endswith("/clang++")
    run_ninja(build)
    for exe in ("cprog", "cxxprog"):
        path = str(build / "souffle" / "bin" / rel / exe)
        assert subprocess.check_output([path], text=True) == "42\n"
        deps = subprocess.check_output(["otool", "-L", path], text=True)
        assert ("libc++" in deps) == (exe == "cxxprog")
    names = {p.name for p in (build / "souffle" / "lib" / rel).iterdir()}
    assert names == {"libz.a", "libz.1.3.dylib", "libz.1.dylib", "libz.dylib"}


@needs_souffle
@pytest.mark.parametrize(
    "spec, message",
    [
        (
            'native_output_name = [["a", "x"], ["b", "x"]]',
            "targets of one kind share an output name",
        ),
        ('native_output_name = [["a", "x"], ["a", "y"]]', "target has two output names"),
        ('native_output_name = [["c", "x"]]', "output name of a target without a native artifact"),
        pytest.param(
            'native_version = [["s", "1.2.3.4", "1"]]',
            "shared library version has more than three components",
            marks=host.macos_only("the X.Y.Z limit is Mach-O's; an ELF file name takes any"),
        ),
    ],
)
def test_output_name_and_version_errors(tmp_path, capsys, spec, message):
    proj = project(
        tmp_path,
        f"""
target = [["a"], ["b"], ["c"], ["s"]]
native_kind = [["a", "library"], ["b", "library"], ["s", "library"]]
default_linkage = [["s", "shared"]]
c_source = [["a", "a.c"], ["b", "a.c"], ["s", "a.c"]]
request = [["a", "rel"], ["b", "rel"], ["s", "rel"]]
{spec}
""",
        {"a.c": "int a(void) { return 1; }\n"},
    )
    assert message in configure_fails(proj, tmp_path, capsys)


# ---- configure checks (zlib case study) -----------------------------------------

CHECKS = """
target = [["app"]]
native_kind = [["app", "executable"]]
c_source = [["app", "main.c"]]
request = [["app", "rel"]]
condition = [
  ["has_int", "$Eq(cc.check.HAVE_INT, yes)"],
  ["no_nothere", "$Eq(cc.check.HAVE_NOTHERE, no)"],
  ["has_stdio", "$Eq(cc.check.HAVE_STDIO_H, yes)"],
]
prop_when = [
  ["app", "cc.define", "INT_OK", "private", "has_int"],
  ["app", "cc.define", "NO_NOTHERE", "private", "no_nothere"],
  ["app", "cc.define", "STDIO_OK", "private", "has_stdio"],
]

[cc_check.HAVE_INT]
lang = "c"
kind = "type"
subject = "int"
cc_check_headers = "stddef.h"

[cc_check.HAVE_NOTHERE]
lang = "c"
kind = "function"
subject = "buildlog_no_such_function"

[cc_check.HAVE_STDIO_H]
lang = "c"
kind = "include"
subject = "stdio.h"

[cc_check.SAME_AS_STDIO]
lang = "c"
kind = "include"
subject = "stdio.h"
"""


@needs_souffle
def test_configure_checks_are_cached_discovery_facts(tmp_path):
    """Each check kind asks the configuration's C toolchain; the answers are
    probe results (the type's size too) that conditions read as cc.check.<name>.
    Two names asking the same question share one probe run, and a
    reconfigure answers every check from the cache."""
    proj = project(tmp_path, CHECKS, {"main.c": "int main(void) { return 0; }\n"})
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    cmd = cmd_of(build, "app/main.c.o")
    assert "-DINT_OK" in cmd and "-DNO_NOTHERE" in cmd and "-DSTDIO_OK" in cmd
    env = build / "env-facts"
    results = {cc_check_fields(r[0])[5]: r[1] for r in read_rows(env / "res_cc_check.facts")}
    assert results == {"int": "yes", "buildlog_no_such_function": "no", "stdio.h": "yes"}
    assert [r[1] for r in read_rows(env / "res_cc_check_size.facts")] == ["4"]
    cache = json.loads((build / "probe-cache.json").read_text())
    assert sum(json.loads(k)[0] == "cc_check" for k in cache) == 3
    _, again = configure_project(proj, build)
    assert again["executed"] == 0


@needs_souffle
@pytest.mark.parametrize(
    "spec, message",
    [
        (
            '[cc_check.X]\nlang = "c"\nkind = "member"\nsubject = "s"',
            "unknown check kind",
        ),
        (
            '[cc_check.X]\nlang = "fortran"\nkind = "include"\nsubject = "s"',
            "check language is not c or cxx",
        ),
        (
            '[cc_check.X]\nlang = "c"\nkind = "function"\nsubject = "f"\ncc_check_headers = "a.h"',
            "function check with a prelude",
        ),
        ('cc_check_args = [["X", "-DY"]]', "check prelude or arguments of an unstated check"),
    ],
)
def test_configure_check_errors(tmp_path, capsys, spec, message):
    proj = project(
        tmp_path,
        f"""
target = [["app"]]
native_kind = [["app", "executable"]]
c_source = [["app", "main.c"]]
request = [["app", "rel"]]
{spec}
""",
        {"main.c": "int main(void) { return 0; }\n"},
    )
    assert message in configure_fails(proj, tmp_path, capsys)


@needs_souffle
def test_a_c_check_in_a_cxx_only_project(tmp_path):
    """A check requires its language's toolchain, so a C check's attribute
    is defined in a project without C sources."""
    proj = project(
        tmp_path,
        """
target = [["app"]]
native_kind = [["app", "executable"]]
cxx_source = [["app", "main.cpp"]]
request = [["app", "rel"]]
condition = [["has_stdio", "$Eq(cc.check.HAVE_STDIO_H, yes)"]]
prop_when = [["app", "cc.define", "STDIO_OK", "private", "has_stdio"]]

[cc_check.HAVE_STDIO_H]
lang = "c"
kind = "include"
subject = "stdio.h"
""",
        {"main.cpp": "int main() { return 0; }\n"},
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    assert "-DSTDIO_OK" in cmd_of(build, "app/main.cpp.o")


# ---- the configuration's standard flags, per target ---------------------------


@needs_souffle
def test_config_flags_wipe_and_link_are_per_target_opt_ins(tmp_path):
    proj = project(
        tmp_path,
        """
target = [["plain"], ["linked"], ["wiped"], ["defaults"]]
native_kind = [["plain", "executable"], ["linked", "executable"], ["wiped", "executable"],
  ["defaults", "interface_library"]]
cxx_source = [["plain", "main.cpp"], ["linked", "main.cpp"], ["wiped", "main.cpp"]]
dep = [["wiped", "defaults", "private"]]
prop = [["linked", "native.link_config_flags", "on", "private"],
  ["wiped", "native.link_config_flags", "on", "private"],
  ["defaults", "native.wipe_config_flag", "-DNDEBUG", "interface"],
  ["defaults", "native.wipe_config_flag", "-O.*", "interface"]]
request = [["plain", "rel"], ["linked", "rel"], ["wiped", "rel"]]
""",
        {"main.cpp": "int main() { return 0; }\n"},
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    flags = (" -O3 ", " -DNDEBUG ")
    obj = lambda t: cmd_of(build, f"{t}/main.cpp.o")
    link = lambda t: cmd_of(build, f"bin/arm64-macos.release.none/{t}")
    assert all(f in obj("plain") and f in obj("linked") for f in flags)
    assert not any(f in link("plain") for f in flags)
    assert all(f in link("linked") for f in flags)
    assert not any(f in obj("wiped") or f in link("wiped") for f in flags)
