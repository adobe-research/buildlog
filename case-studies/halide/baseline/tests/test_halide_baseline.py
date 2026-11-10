# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Halide case-study baseline tooling (normalize.py, compare.py).

These use the miniature CMake-shaped fixture in fixtures/mini (fake roots /work/src and
/work/build) and never need a Halide build. The last test re-normalizes the real raw
baseline and is skipped unless baseline/raw/release exists.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE = os.path.dirname(HERE)
MINI = os.path.join(HERE, "fixtures", "mini")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"halide_{name}", os.path.join(BASELINE, f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


normalize = _load("normalize")
compare = _load("compare")

MACHINE_PATH = re.compile(r"/Users/|/opt/homebrew|/Applications/|/Library/Developer|/work/")


def mini_records(full: bool = True) -> list[dict]:
    nf = normalize.parse_ninja(os.path.join(MINI, "build.ninja"))
    fa = normalize.load_fileapi(os.path.join(MINI, "fileapi"), "/work/src", "/work/build")
    roots = normalize.Roots(
        {
            "$BUILD": ["/work/build"],
            "$SRC": ["/work/src"],
            "$LLVM": ["/opt/homebrew/opt/llvm@22"],
            "$HOMEBREW": ["/opt/homebrew", "/work/cmake"],
        }
    )
    recs = normalize.normalize(
        nf, "/work/build", roots, fa, normalize.load_scope(os.path.join(MINI, "scope.toml")), "mini"
    )
    return recs if full else [normalize.slim(r) for r in recs]


def by_id(recs: list[dict]) -> dict[str, dict]:
    return {r["id"]: r for r in recs}


# --------------------------------------------------------------------------- ninja parsing


def test_parse_eval_escapes():
    pieces = normalize.parse_eval("a$ b$:c$$d ${x}y", split_paths=True)
    assert pieces == [[(False, "a b:c$d")], [(True, "x"), (False, "y")]]


def test_parse_ninja_include_and_scopes():
    nf = normalize.parse_ninja(os.path.join(MINI, "build.ninja"))
    assert "CUSTOM_COMMAND" in nf.rules  # from the included rules.ninja
    assert nf.defaults == ["all"]
    link = next(e for e in nf.edges if e.outputs == ["src/libHalide.22.0.0.dylib"])
    cmd = normalize.edge_var(nf, link, "command")
    assert "-install_name @rpath/libHalide.22.dylib" in cmd
    assert link.implicit == ["/opt/homebrew/opt/llvm@22/lib/libLLVM.dylib"]


# --------------------------------------------------------------------------- decomposition


def test_decompose_cd_and_redirections():
    steps = normalize.decompose("cd /b/rt && /b/tools/binary2cpp name < /s/x.bc > out.cpp", "/b")
    assert steps == [
        {
            "cwd": "/b/rt",
            "argv": ["/b/tools/binary2cpp", "name"],
            "stdin": "/s/x.bc",
            "stdout": "out.cpp",
        }
    ]


def test_decompose_sh_c_and_cmake_env():
    (s,) = normalize.decompose("/bin/sh -c 'cd /a && foo x > y'", "/b")
    assert s["wrapper"] == ["/bin/sh", "-c"] and s["argv"] == ["foo", "x"] and s["cwd"] == "/a"
    (s,) = normalize.decompose("cd /w && /c/cmake -E env FOO=1 -- /usr/bin/tool a", "/b")
    assert s["env"] == {"FOO": "1"} and s["argv"] == ["/usr/bin/tool", "a"]


def test_roots_longest_prefix_and_boundaries():
    r = normalize.Roots({"$SRC": ["/w/src"], "$BUILD": ["/w/src/build"]})
    assert r.sub("/w/src/build/a.o /w/src/a.cpp") == "$BUILD/a.o $SRC/a.cpp"
    assert r.sub("/w/src-other/x") == "/w/src-other/x"  # not a path-component match


def test_roots_short_root_does_not_match_inside_a_path():
    # The Linux container's roots are short (/src, /build/...): /src must not match
    # the src/ component of a build path, but still after flags and separators.
    r = normalize.Roots({"$SRC": ["/src"], "$BUILD": ["/build/cmake-x"]})
    assert r.sub("/build/cmake-x/src/a.o") == "$BUILD/src/a.o"
    assert r.sub("/build/cmake-x/src/a.o;/src/b") == "$BUILD/src/a.o;$SRC/b"
    assert r.sub("-I/src/src -isystem/src/d -L/build/cmake-x/lib") == (
        "-I$SRC/src -isystem$SRC/d -L$BUILD/lib"
    )
    assert r.sub("-Wl,-rpath,/build/cmake-x:/src/x --x=/src") == (
        "-Wl,-rpath,$BUILD:$SRC/x --x=$SRC"
    )


def test_hashed_depfile_is_canonicalized():
    r = normalize.Roots({"$BUILD": ["/w/b"]})
    s = "/w/b/CMakeFiles/d/" + "ab" * 32 + ".d"
    assert normalize.canon_str(r, s) == "$BUILD/CMakeFiles/d/<sha256>.d"


# --------------------------------------------------------------------------- scope rules


def test_braces_and_rules():
    assert normalize.gmatch("install/local", "{install,install/local}")
    assert not normalize.gmatch(None, "*")
    rules = [{"dir": "python*", "exclude": "py"}, {"kind": "compile", "phase": 1}]
    assert normalize.apply_rules({"outputs": [], "dir": "python_x"}, rules, set()) == (None, "py")
    assert normalize.apply_rules({"outputs": [], "kind": "compile"}, rules, set()) == (1, None)
    assert normalize.apply_rules({"outputs": [], "kind": "link"}, rules, set()) == (
        None,
        "unmapped",
    )


def test_sample_closure_follows_fileapi_deps():
    fa = normalize.load_fileapi(os.path.join(MINI, "fileapi"), "/work/src", "/work/build")
    scope = {"tests": {"correctness": ["Halide"]}}
    assert normalize.sample_closure(scope, fa) == {"Halide", "Halide_initmod", "binary2cpp"}


# --------------------------------------------------------------------------- normalize (fixture)


def test_normalize_kinds_targets_phases():
    r = by_id(mini_records())
    obj = r["$BUILD/src/CMakeFiles/Halide.dir/Func.cpp.o"]
    assert (obj["kind"], obj["target"], obj["phase"], obj["target_via"]) == (
        "compile",
        "Halide",
        1,
        "fileapi-objdir",
    )
    assert "-isystem" in obj["steps"][0]["argv"] and "$LLVM/include" in obj["steps"][0]["argv"]
    link = r["$BUILD/src/libHalide.22.0.0.dylib"]
    assert link["kind"] == "link" and link["link_type"] == "shared"
    assert link["inputs"]["implicit"] == ["$LLVM/lib/libLLVM.dylib"]
    sym = r["$BUILD/src/libHalide.22.dylib"]
    assert sym["kind"] == "symlink" and sym["target"] == "Halide"
    assert sym["outputs"] == ["$BUILD/src/libHalide.22.dylib", "$BUILD/src/libHalide.dylib"]
    assert r["$BUILD/tools/binary2cpp"]["phase"] == 2
    assert r["$BUILD/Halide"]["phony"] and r["$BUILD/Halide"]["target"] == "Halide"


def test_classify_gcc_and_clang_pch():
    step = lambda *argv: [{"argv": list(argv), "cwd": "/b"}]
    gcc = step("/usr/bin/c++", "-x", "c++-header", "-o", "p.hxx.gch", "-c", "p.hxx.cxx")
    clang = step("c++", "-Xclang", "-emit-pch", "-x", "c++-header", "-c", "p.hxx.cxx")
    assert normalize.classify("CXX_COMPILER", gcc, 0, False) == ("pch", "")
    assert normalize.classify("CXX_COMPILER", clang, 0, False) == ("pch", "")
    plain = step("/usr/bin/c++", "-x", "c++", "-c", "a.cpp")
    assert normalize.classify("CXX_COMPILER", plain, 0, False) == ("compile", "")


def test_classify_ln_symlink():
    step = lambda *argv: [{"argv": list(argv), "cwd": "/b"}]
    assert (
        normalize.classify("run", step("ln", "-sf", "a.dylib", "b.dylib"), 0, False)[0] == "symlink"
    )
    assert normalize.classify("run", step("ln", "a", "b"), 0, False)[0] != "symlink"


def test_normalize_custom_command_host_tool():
    rec = by_id(mini_records())["$BUILD/src/runtime/_initmod_posix.cpp"]
    assert rec["kind"] == "custom" and rec["phase"] == 3
    assert rec["target"] == "Halide_initmod" and rec["target_via"] == "fileapi-source"
    assert rec["tool_name"] == "binary2cpp" and rec["tool_built"] is True
    (step,) = rec["steps"]
    assert step["stdin"] == "$SRC/src/runtime/posix.bc"
    assert step["stdout"] == "$BUILD/src/runtime/_initmod_posix.cpp"  # made absolute
    assert rec["argv"][:3] == ["cd", "$BUILD/src/runtime", "&&"]  # original kept
    assert rec["outputs"] == ["$BUILD/src/runtime/_initmod_posix.cpp"]


def test_normalize_exclusions_and_slim():
    recs = mini_records(full=False)
    r = by_id(recs)
    assert r["$BUILD/python_bindings/CMakeFiles/Halide_py_thing.util"]["excluded"].startswith(
        "python"
    )
    assert r["$BUILD/build.ninja"]["excluded"] == "regeneration edge"
    assert r["$BUILD/CMakeFiles/edit_cache.util"]["kind"] == "meta"
    slim = r["$BUILD/python_bindings/CMakeFiles/Halide_py_thing.util"]
    assert set(slim) <= set(normalize._KEEP_WHEN_EXCLUDED)


def test_normalize_is_sorted_stable_and_path_free():
    a, b = mini_records(), mini_records()
    assert a == b
    assert [x["id"] for x in a] == sorted(x["id"] for x in a)
    text = "\n".join(json.dumps(x, sort_keys=True) for x in a)
    assert not MACHINE_PATH.search(text), MACHINE_PATH.search(text)


def test_cli_test_regex(capsys):
    assert normalize.main(["test-regex", "--scope", os.path.join(BASELINE, "scope.toml")]) == 0
    rx = re.compile(capsys.readouterr().out.strip())
    assert rx.match("correctness_bounds") and rx.match("generator_aot_alias")
    assert not rx.match("correctness_bounds_x")


# --------------------------------------------------------------------------- compare


def test_units_and_set_compare():
    a = [
        "c++",
        "-DA",
        "-O3",
        "-I",
        "x",
        "-Iy",
        "-Xclang",
        "-include",
        "-Xclang",
        "h.h",
        "-c",
        "f.cpp",
    ]
    b = [
        "c++",
        "-c",
        "f.cpp",
        "-O3",
        "-Xclang",
        "-include",
        "-Xclang",
        "h.h",
        "-D",
        "A",
        "-Ix",
        "-I",
        "y",
    ]
    assert compare.diff_set(a, b) is None
    swapped = [
        "c++",
        "-DA",
        "-O3",
        "-Iy",
        "-Ix",
        "-Xclang",
        "-include",
        "-Xclang",
        "h.h",
        "-c",
        "f.cpp",
    ]
    d = compare.diff_set(a, swapped)
    assert d and "include_order" in d  # include search order matters
    d = compare.diff_set(a, ["c++", "-DB", "-O3", "-Ix", "-Iy", "-c", "f.cpp"])
    assert d["only_a"] == ["-D A", "-Xclang -include -Xclang h.h"] and d["only_b"] == ["-D B"]
    assert compare.diff_set(a, ["clang++", *a[1:]])["tool"] == ["c++", "clang++"]
    # Include order is still reported when the flag sets differ as well.
    d = compare.diff_set(["c++", "-O3", "-Ix", "-Iy"], ["c++", "-O2", "-Iy", "-Iz", "-Ix"])
    assert d["include_order"] == [["-I x", "-I y"], ["-I y", "-I x"]]
    assert "include_order" not in compare.diff_set(["c++", "-O3", "-Ix"], ["c++", "-O2", "-Ix"])


def test_ordered_compare_for_links():
    a = ["c++", "a.o", "b.o", "-o", "x"]
    assert compare.diff_argv("link", a, a, compare.DEFAULT_MODES) is None
    d = compare.diff_argv("link", a, ["c++", "b.o", "a.o", "-o", "x"], compare.DEFAULT_MODES)
    assert d["ops"]
    assert compare.diff_argv("link", a, ["c++", "b.o", "a.o", "-o", "x"], {"link": "set"}) is None


def test_compare_counts_per_phase_kind():
    a = mini_records()
    b = copy.deepcopy(a)
    b = [r for r in b if r["id"] != "$BUILD/src/CMakeFiles/Halide.dir/Func.cpp.o"]
    extra = copy.deepcopy(by_id(a)["$BUILD/tools/binary2cpp"])
    extra["id"] = extra["outputs"][0] = "$BUILD/tools/other"
    b.append(extra)
    res = compare.compare(a, b)
    assert res.rows[(1, "compile")]["only_a"] == 1
    assert res.rows[(2, "link")]["only_b"] == 1
    assert res.only_a == ["$BUILD/src/CMakeFiles/Halide.dir/Func.cpp.o"]
    out = compare.report(res)
    assert "only in A" in out and "only in B" in out


def test_path_map_and_fallback_output_match():
    a = mini_records()
    pm = compare.PathMap(rules=[("b", re.compile(r"\$BUILD/"), "$BUILD/out/")])
    moved = [compare.map_record(r, "b", pm) for r in a]
    assert compare.compare(a, moved).totals()["match"] == 0
    back = compare.PathMap(rules=[("b", re.compile(r"\$BUILD/out/"), "$BUILD/")])
    t = compare.compare(a, moved, pm=back).totals()
    assert t["differ"] == 0 and t["only_a"] == 0 and t["only_b"] == 0
    # A record whose id differs but which shares an output is still matched.
    b = copy.deepcopy(a)
    sym = next(r for r in b if r["id"] == "$BUILD/src/libHalide.22.dylib")
    sym["id"] = "$BUILD/src/libHalide.dylib"
    sym["outputs"] = sym["outputs"][::-1]
    assert compare.compare(a, b).totals()["only_a"] == 0


def test_path_map_toml():
    pm = compare.PathMap.load(os.path.join(HERE, "fixtures", "pathmap.toml"))
    assert pm.apply("b", "$BUILD/obj/src/Func.o") == "$BUILD/src/CMakeFiles/Halide.dir/Func.cpp.o"
    assert pm.modes == {"custom": "set"}
    assert pm.ignore[0].fullmatch("-fcolor-diagnostics")
    assert pm.ignore_flags == {"-MT"}
    assert compare._drop_flags(["c++", "-MT", "x.o", "-MD", "-c", "f.cpp"], pm.ignore_flags) == [
        "c++",
        "-MD",
        "-c",
        "f.cpp",
    ]


def test_self_test_on_fixture():
    buf = io.StringIO()
    assert compare.self_test(mini_records(), out=buf), buf.getvalue()
    assert "FAIL" not in buf.getvalue()


# --------------------------------------------------------------------------- checked-in baselines


BASELINES = sorted(
    f for f in os.listdir(BASELINE) if f.startswith("cmake-") and f.endswith(".jsonl")
)


@pytest.mark.parametrize("name", BASELINES)
def test_checked_in_baseline_has_no_machine_paths(name):
    with open(os.path.join(BASELINE, name), encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"/Users/|/opt/homebrew|/Applications/|/Library/Developer|\$HOME(?!BREW)", text)
    assert m is None, text[max(0, m.start() - 80) : m.end() + 80]
    lines = text.splitlines()
    ids = [(r.get("id") or r["path"]) for r in map(json.loads, lines)]
    assert ids == sorted(ids)


@pytest.mark.parametrize(
    "name",
    ["cmake-release.jsonl", "cmake-linux-gcc-release.jsonl", "cmake-cross-linux-aarch64.jsonl"],
)
def test_release_baseline_self_test(name):
    path = os.path.join(BASELINE, name)
    if not os.path.exists(path):
        pytest.skip(f"{name} not generated")
    buf = io.StringIO()
    assert compare.self_test(compare.load_jsonl(path), out=buf), buf.getvalue()


def test_real_raw_baseline_renormalizes_identically():
    raw = os.path.join(BASELINE, "raw", "release")
    if not os.path.exists(os.path.join(raw, "build.ninja")):
        pytest.skip("needs the raw Halide CMake baseline (run baseline/run-cmake.sh release)")
    with open(os.path.join(raw, "CMakeCache.txt"), encoding="utf-8") as f:
        cache = f.read()
    build = re.search(r"^CMAKE_CACHEFILE_DIR:INTERNAL=(.*)$", cache, re.MULTILINE).group(1)
    src = re.search(r"^Halide_SOURCE_DIR:STATIC=(.*)$", cache, re.MULTILINE).group(1)
    llvm = "/opt/homebrew/opt/llvm@22"
    nf = normalize.parse_ninja(os.path.join(raw, "build.ninja"))
    fa = normalize.load_fileapi(os.path.join(raw, "fileapi-reply"), src, build)
    recs = normalize.normalize(
        nf,
        build,
        normalize.default_roots(src, build, llvm),
        fa,
        normalize.load_scope(os.path.join(BASELINE, "scope.toml")),
        "release",
    )
    got = [json.dumps(normalize.slim(r), sort_keys=True, separators=(",", ":")) for r in recs]
    with open(os.path.join(BASELINE, "cmake-release.jsonl"), encoding="utf-8") as f:
        assert got == f.read().splitlines()


# --------------------------------------------------------------------------- metrics

metrics = _load("metrics")


def test_sloc_skips_blank_and_comments():
    assert metrics.sloc(os.path.join(MINI, "CMakeLists.txt")) == 4


def test_scope_tables_on_fixture():
    md = metrics.scope_tables([normalize.slim(r) for r in mini_records()])
    assert "| 3 |" in md and "`binary2cpp` | built in this graph | 1 | 1 |" in md
    assert "| regeneration edge | 1 |" in md


def test_ctoken_keeps_non_paths():
    nf = normalize.parse_ninja(os.path.join(MINI, "build.ninja"))
    roots = normalize.Roots({"$BUILD": ["/work/build"], "$SRC": ["/work/src"]})
    norm = normalize.Normalizer(nf, "/work/build", roots)
    cwd = "/work/build/src"
    assert norm.ctoken(".fbs", cwd) == ".fbs"  # flatc --filename-suffix .fbs
    assert norm.ctoken("C++17", cwd) == "C++17"
    assert norm.ctoken("x.ll", cwd) == "$BUILD/src/x.ll"
    assert norm.ctoken("../tools/binary2cpp", cwd) == "$BUILD/tools/binary2cpp"  # known node
    # Relative paths that climb out of the working directory (buildlog's sources).
    assert norm.ctoken("../../src/a.cpp", cwd) == "$SRC/a.cpp"
    assert norm.ctoken("-I../../src/include", cwd) == "-I$SRC/include"
    assert norm.ctoken("-isystem../../src/x", cwd) == "-isystem$SRC/x"
    assert norm.ctoken("-Ifoo", cwd) == "-Ifoo"
