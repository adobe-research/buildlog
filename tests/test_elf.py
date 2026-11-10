# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""ELF conventions of buildlog-cpp on Linux (GCC or clang), the counterpart
of test_cpp_mechanisms.py's Mach-O tests: a versioned shared library is
lib<o>.so.<version> with its soname (lib<o>.so.<soversion>) and the -l link
lib<o>.so, programs find it through a $ORIGIN rpath, a whole archive is
bracketed by --whole-archive/--no-whole-archive in one word, and a compiler's
own include dirs are never named again."""

from __future__ import annotations

import os
import subprocess

import pytest

from tests import host
from tests.test_cpp_mechanisms import (
    cmd_of,
    configure,
    needs_souffle,
    ninja_text,
    project,
    run_ninja,
)

pytestmark = [needs_souffle, pytest.mark.skipif(not host.LINUX, reason="Linux only: ELF")]

REL = "arm64-macos.release.none"  # linux/port.py renames it to the host's


def _dynamic(path) -> str:
    return subprocess.check_output(["readelf", "-d", str(path)], text=True)


def test_a_versioned_shared_library_has_its_soname_links_and_runs(tmp_path):
    proj = project(
        tmp_path,
        """
target = [["app"], ["answer"]]
native_kind = [["app", "executable"], ["answer", "library"]]
default_linkage = [["answer", "shared"]]
cxx_source = [["app", "main.cpp"], ["answer", "answer.cpp"]]
native_version = [["answer", "3.1.0", "3"]]
dep = [["app", "answer", "private"]]
request = [["app", "rel"]]
""",
        {
            "main.cpp": '#include <cstdio>\nint answer();\nint main() { std::printf("%d\\n", answer()); }\n',
            "answer.cpp": "int answer() { return 42; }\n",
        },
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    link = cmd_of(build, f"lib/{REL}/libanswer.so.3.1.0")
    assert " -shared " in link and "-Wl,-soname,libanswer.so.3" in link
    assert " -fPIC " in cmd_of(build, "answer.cpp.o")
    assert cmd_of(build, f"bin/{REL}/").endswith(
        f"libanswer.so.3.1.0 '-Wl,-rpath,$$ORIGIN/'../../lib/{REL}"
    )
    run_ninja(build)
    lib = build / "souffle" / "lib" / REL
    assert os.readlink(lib / "libanswer.so.3") == "libanswer.so.3.1.0"
    assert os.readlink(lib / "libanswer.so") == "libanswer.so.3"
    assert "Library soname: [libanswer.so.3]" in _dynamic(lib / "libanswer.so.3.1.0")
    exe = build / "souffle" / "bin" / REL / "app"
    dyn = _dynamic(exe)
    assert "[libanswer.so.3]" in dyn and f"[$ORIGIN/../../lib/{REL}]" in dyn
    assert subprocess.check_output([str(exe)], text=True) == "42\n"


def test_a_whole_archive_is_bracketed_in_one_word(tmp_path):
    proj = project(
        tmp_path,
        """
target = [["app"], ["gen"], ["plugin_api"]]
native_kind = [["app", "executable"], ["gen", "library"]]
cxx_source = [["app", "main.cpp"], ["gen", "gen.cpp"]]
dep = [["plugin_api", "gen", "public"], ["app", "plugin_api", "private"]]
whole_archive = [["plugin_api", "gen"]]
request = [["app", "rel"]]
""",
        {
            "gen.cpp": '#include <cstdio>\nstruct R { R() { std::puts("registered"); } } r;\n',
            "main.cpp": "int main() { return 0; }\n",
        },
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    app = cmd_of(build, f"bin/{REL}/app")
    assert f" -Wl,--whole-archive,lib/{REL}/libgen.a,--no-whole-archive" in app
    run_ninja(build)
    exe = build / "souffle" / "bin" / REL / "app"
    assert subprocess.check_output([str(exe)], text=True) == "registered\n"


def test_the_compilers_own_include_dirs_are_not_named(tmp_path):
    """An imported package whose include dir is /usr/include (a pkg-config
    file of the distribution) adds no -isystem /usr/include: GCC's <cstdlib>
    would then find no <stdlib.h> through #include_next."""
    proj = project(
        tmp_path,
        """
target = [["app"], ["sys"]]
native_kind = [["app", "executable"]]
cxx_source = [["app", "main.cpp"]]
prop = [["sys", "cc.include_dir", "/usr/include", "interface"],
  ["sys", "cc.include_dir", "src:inc", "interface"]]
dep = [["app", "sys", "private"]]
request = [["app", "rel"]]
""",
        {"main.cpp": "#include <cstdlib>\n#include <mine.h>\nint main() { return MINE; }\n"},
    )
    (proj / "src" / "inc").mkdir()
    (proj / "src" / "inc" / "mine.h").write_text("#define MINE 0\n")
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    compile_ = cmd_of(build, f"obj/{REL}/app/main.cpp.o")
    assert "/usr/include" not in compile_ and "-isystem ../../proj/src/inc" in compile_
    run_ninja(build)


def test_a_version_script_limits_the_exports(tmp_path):
    proj = project(
        tmp_path,
        """
target = [["answer"]]
native_kind = [["answer", "library"]]
default_linkage = [["answer", "shared"]]
cxx_source = [["answer", "answer.cpp"]]
native_version_script = [["answer", "src:exports.ld"]]
request = [["answer", "rel"]]
""",
        {
            "answer.cpp": 'extern "C" int shown() { return 1; }\nextern "C" int hidden() { return 2; }\n',
            "exports.ld": "{ global: shown; local: *; };\n",
        },
    )
    build, metrics = configure(proj, tmp_path)
    assert metrics["errors"] == []
    link = cmd_of(build, f"lib/{REL}/libanswer.so")
    assert "-Wl,--version-script=" in link and "exports.ld" in link
    stmt = next(
        ln
        for ln in ninja_text(build).splitlines()
        if ln.startswith("build ") and "libanswer.so:" in ln
    )
    assert "exports.ld" in stmt.split("|", 1)[1]  # an implicit input of the link
    run_ninja(build)
    syms = subprocess.check_output(
        ["nm", "-D", "--defined-only", str(build / "souffle" / "lib" / REL / "libanswer.so")],
        text=True,
    )
    assert " shown" in syms and " hidden" not in syms
