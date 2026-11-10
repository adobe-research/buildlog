# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The `buildlog` export format: an installed package as Datalog
(src/buildlog/dl_package.py, buildlog_cpp/importers/buildlog_dl.py).

Install writes `<prefix>/share/buildlog/<pkg>/` (manifest.toml, `<pkg>.facts.dl`
and the layers of the project's own rule package); a downstream finds it with
the `buildlog` provider, which also loads the package as a rule package.
"""

from __future__ import annotations

import csv
import difflib
import json
import os
import runpy
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from buildlog_cpp import importers

from buildlog import cli, dl_package
from buildlog.engine import configure_project
from buildlog.errors import LoaderError, SpecError
from buildlog.paths import souffle_path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
PKGX = FIXTURES / "export-components"
PKGY = FIXTURES / "export-requires"
DSL = FIXTURES / "export-rules"
PRESET = ROOT / "case-studies" / "nlohmann-json" / "project" / "presets" / "apple-clang-arm64.toml"
REL = "arm64-macos.release.none"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed"),
    pytest.mark.skipif(shutil.which("ninja") is None, reason="ninja not installed"),
]

RESULTS = {
    "res_platform_constraint": {
        ("r", "p", "os", "macos"),
        ("r", "p", "cpu", "arm64"),
        ("r", "p", "cpu", "x86_64"),
    }
}


# ---- building, installing, importing ---------------------------------------------


def copy_project(src: Path, dst: Path) -> Path:
    shutil.copytree(
        src, dst, ignore=shutil.ignore_patterns("buildlog-build", "buildlog.lock", "__pycache__")
    )
    return dst


def configure(project: Path, build: Path, text: str, **kw) -> dict:
    user = build.parent / f"{build.name}-user.toml"
    user.write_text(text)
    _, metrics = configure_project(project, build, user_files=[PRESET, user], **kw)
    assert metrics["errors"] == []
    return metrics


def ninja(build: Path, *args: str) -> str:
    cp = subprocess.run(
        ["ninja", "-C", str(build / "souffle"), *args], check=True, capture_output=True, text=True
    )
    return cp.stdout


def install(project: Path, tmp: Path, name: str, bind: str = "") -> tuple[Path, Path]:
    """Build and install `project` under tmp/name; (build, prefix)."""
    prefix, build = tmp / name, tmp / f"b-{name}"
    configure(
        copy_project(project, tmp / f"src-{name}"), build, f'install_prefix = "{prefix}"\n{bind}'
    )
    ninja(build, "install")
    return build, prefix


@pytest.fixture(scope="module")
def pkgx(tmp_path_factory):
    """(build, prefix) of buildlog's install of pkgx 1.2.3 (two components)."""
    return install(PKGX / "project", tmp_path_factory.mktemp("x"), "x")


def search(fmt: str, prefix: Path) -> str:
    sub = {"buildlog": "share/buildlog", "cps": "share/cps"}[fmt]
    return f'["{fmt}", "{prefix}/{sub}"]'


@pytest.fixture(scope="module")
def pkgy(tmp_path_factory, pkgx):
    """(build, prefix) of pkgy 0.1.0, which requires pkgx:b, bound through
    the `buildlog` route."""
    bind = f"package_search_dir = [{search('buildlog', pkgx[1])}]\n"
    return install(PKGY / "project", tmp_path_factory.mktemp("y"), "y", bind)


def facts_file(prefix: Path, pkg: str) -> Path:
    return prefix / "share" / "buildlog" / pkg / f"{pkg}.facts.dl"


def importing(fmt: str, path: Path) -> set[tuple[str, tuple[str, ...]]]:
    """(relation, row) of one import, request id dropped."""
    out = importers.pkg_import(None, "r", (fmt, str(path)), RESULTS)
    return {
        (rel.removeprefix("res_pkg_"), row[1:])
        for rel, rows in out.items()
        if rel != "__origin__"
        for row in rows
    }


# ---- the facts the build derives (the exporter's side) ---------------------------


def last_round(build: Path) -> Path:
    return max((build / "facts-in").glob("round-*"), key=lambda p: int(p.name.split("-")[1]))


def stated(build: Path, rel: str) -> list[list[str]]:
    path = last_round(build) / f"{rel}.facts"
    return [r.split("\t") for r in path.read_text().splitlines()] if path.exists() else []


def derived(build: Path, rel: str) -> list[list[str]]:
    path = build / "souffle" / "facts" / f"{rel}.csv"
    with open(path) as f:
        return list(csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)) if path.exists() else []


def exported(build: Path, up: int = 3) -> set[tuple[str, tuple[str, ...]]]:
    """E: what the exporter derives for the package, in the vocabulary of the
    file (L3_install.dl's export_* relations, one by one)."""
    out: set[tuple[str, tuple[str, ...]]] = set()
    (pkg,) = {r[0] for r in stated(build, "export_package")}
    out.add(("pkg_package", (pkg,)))
    out.add(("pkg_prefix", ("/".join([".."] * up),)))
    out |= {("pkg_version", (p, v)) for p, v in stated(build, "export_version")}
    out |= {("pkg_description", (p, v)) for p, v in stated(build, "export_description")}
    out |= {("pkg_compat", (p, v)) for p, v in derived(build, "export_compat_in")}
    native = {(p, m): y for p, m, _, _, y, _, _ in derived(build, "export_native")}
    for p, m, _ in stated(build, "export_component"):
        out.add(("pkg_component", (m, native.get((p, m), "interface"))))
        out.add(("pkg_default", (m,)))
    rel = {"path": "pkg_prop_path", "abspath": "pkg_prop_abs", "value": "pkg_prop"}
    out |= {(rel[kind], (m, k, v)) for _, m, _, kind, k, v in derived(build, "export_fact")}
    out |= {("pkg_requires", (m, n)) for _, m, _, n in derived(build, "export_requires")}
    out |= {
        ("pkg_depends", (m, n, k)) for _, m, _, n, k in derived(build, "export_requires_package")
    }
    out |= {
        ("pkg_requires_version", (n, v))
        for _, _, n, v, _ in derived(build, "export_requires_version_from")
    }
    return out


def parsed(path: Path) -> set[tuple[str, tuple[str, ...]]]:
    rows = [(rel, row) for rel, row, _ in dl_package.parse_facts(path.read_text())]
    assert len(rows) == len(set(rows)), "a fact is stated twice"
    return set(rows)


# ---- the round trip ---------------------------------------------------------------


def import_rows(pkg: str, prefix: Path, e: set) -> set[tuple[str, tuple[str, ...]]]:
    """What the importer must state for the facts `e`, at `prefix`: the
    translation the format documents, written out independently of the
    importer (the target ids of CPS, the prefix joined to each prefix-relative
    path, the package's default components, the version in three fields)."""
    by: dict[str, list[tuple[str, ...]]] = {}
    for rel, row in e:
        by.setdefault(rel, []).append(row)
    out: set[tuple[str, tuple[str, ...]]] = set()
    t = lambda c: f"{pkg}:{c}"
    version = by["pkg_version"][0][1] if "pkg_version" in by else None
    vparts = tuple((version.split(".", 2) + ["0", "0"])[:3]) if version else None
    comps = {c: k for c, k in by["pkg_component"]}
    for c, kind in comps.items():
        out |= {
            ("target", (t(c), "interface" if kind == "interface" else "library")),
            ("package", (t(c), pkg, c)),
        }
        if vparts:
            out.add(("version", (t(c), *vparts)))
    for (c,) in by["pkg_default"]:
        out.add(("dep", (t("*"), t(c), "public", "")))
    out |= {
        ("target", (t("*"), "interface")),
        ("package", (t("*"), pkg, "")),
        ("alias", (pkg, t("*"))),
    }
    if vparts:
        out.add(("version", (t("*"), *vparts)))
    out |= {("dep", (t(c), t(o), "public", "")) for c, o in by.get("pkg_requires", [])}
    out |= {
        ("dep", (t(c), f"{p}:{o or '*'}", "public", "")) for c, p, o in by.get("pkg_depends", [])
    }
    out |= {("prop", (t(c), k, v, "")) for c, k, v in by.get("pkg_prop", [])}
    out |= {("prop", (t(c), k, str(prefix / r), "")) for c, k, r in by.get("pkg_prop_path", [])}
    return out


# Rows only the buildlog importer states: where the package's rules are, and
# attributes of native components (CPS has no field for them).
BUILDLOG_ONLY = {"attr"}


def without(rows, kinds):
    return {(k, row) for k, row in rows if k not in kinds}


def test_round_trip_x_and_y(pkgx, pkgy):
    """THE ROUND TRIP. For package P installed under prefix A, let E be the set
    of (relation, tuple) the exporter derives for P: its components, their
    usage requirements (exported properties, by buildlog's own keys), the
    requirements on other packages and the package's metadata, one for one
    from L3_install.dl's `export_*` relations, written in the vocabulary of
    dl_package.VOCABULARY (`exported`, above). Then:

    (1) the facts file at A states exactly E: as a set, and each fact once;
    (2) for any prefix B that holds a copy of the installed tree, the rows the
        importer states for the file under B equal the translation of E that
        the format documents, with each prefix-relative path joined to B
        (`import_rows`); so a copy or move of the install, i.e. B != A, changes
        exactly the path values, by the substitution A -> B.

    Excluded, and said so: `pkg_prop_abs` (an absolute path outside the
    package, which is not relocatable and so is stated as it is: neither
    fixture has one), and the package's compatibility metadata
    (`pkg_compat`, `pkg_description`, `pkg_requires_version`), which (1)
    covers but the importer does not relay: they are not usage requirements
    (the CPS importer skips them too)."""
    for pkg, (build, prefix) in {"pkgx": pkgx, "pkgy": pkgy}.items():
        e = exported(build)
        assert parsed(facts_file(prefix, pkg)) == e
        rows = without(importing("buildlog", facts_file(prefix, pkg)), {"import"})
        assert rows == import_rows(pkg, prefix, e)
    # pkgy's requirement on pkgx is one of the facts, and an edge on import
    assert ("pkg_depends", ("y", "pkgx", "b")) in exported(pkgy[0])
    assert ("dep", ("pkgy:y", "pkgx:b", "public", "")) in importing(
        "buildlog", facts_file(pkgy[1], "pkgy")
    )


def test_relocated_install(tmp_path, pkgx):
    """A copy of the install at another prefix imports as the same rows with
    the prefix substituted, and its files are found there (the prefix comes
    from the file's own location)."""
    _, prefix = pkgx
    moved = tmp_path / "elsewhere" / "deeper"
    shutil.copytree(prefix, moved)
    before = importing("buildlog", facts_file(prefix, "pkgx"))
    after = importing("buildlog", facts_file(moved, "pkgx"))
    assert after != before
    assert {(k, tuple(v.replace(str(moved), str(prefix)) for v in row)) for k, row in after} == {
        (k, tuple(v.replace(str(moved), str(prefix)) for v in row)) for k, row in before
    }
    props = {row[2] for k, row in after if k == "prop" and row[1] == "cc.include_dir"}
    assert props == {f"{moved}/include", f"{moved}/include/pkgx"}
    assert all(Path(p).is_dir() for p in props)
    assert str(prefix) not in repr(sorted(after))


def test_file_is_souffle_datalog(tmp_path, pkgx, pkgy):
    """The export is Datalog, literally: Soufflé accepts the facts file after
    the declarations of the vocabulary and reproduces its tuples."""
    for pkg, (_, prefix) in {"pkgx": pkgx, "pkgy": pkgy}.items():
        text = facts_file(prefix, pkg).read_text()
        header = dl_package.souffle_header()
        outputs = "".join(
            f'.output {rel}(IO=file, filename="{rel}.csv", delimiter="\\t")\n'
            for rel in dl_package.VOCABULARY
        )
        prog = tmp_path / f"{pkg}.dl"
        prog.write_text(header + outputs + text)
        out = tmp_path / f"out-{pkg}"
        out.mkdir()
        cp = subprocess.run(
            [str(souffle_path()), "-D", str(out), str(prog)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert cp.returncode == 0, cp.stderr
        got = set()
        for rel in dl_package.VOCABULARY:
            with open(out / f"{rel}.csv") as f:
                got |= {
                    (rel, tuple(r)) for r in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
                }
        assert got == parsed(facts_file(prefix, pkg))
        assert len(got) > 8


def test_facts_syntax_errors_name_the_line():
    for text, message in [
        ('pkg_package("a")\n', "not a ground fact"),
        ('pkg_unknown("a").\n', "unknown relation"),
        ('pkg_package("a", "b").\n', "takes 1 arguments"),
        ("pkg_package(a).\n", "strings"),
        ('pkg_package("a).\n', "unterminated"),
    ]:
        with pytest.raises(dl_package.FactsError, match=message) as exc:
            dl_package.parse_facts("// ok\n" + text)
        assert exc.value.line == 2
    odd = 'back\\slash "quoted"\ttab'
    assert dl_package.parse_facts(dl_package.fact("pkg_package", odd))[0][1] == (odd,)


# ---- consumption -------------------------------------------------------------------


def downstream(tmp: Path, name: str, src: Path, route: str, prefixes: list[Path], **kw):
    """Configure a copy of the downstream project `src` with its packages
    found through `route` (cps | buildlog | prefix); (project, build)."""
    (tmp / name).mkdir()
    proj, build = copy_project(src / "downstream", tmp / name / "proj"), tmp / name / "b"
    if route == "prefix":
        text = "package_prefix = [" + ", ".join(f'["{p}"]' for p in prefixes) + "]\n"
    else:
        text = "package_search_dir = [" + ", ".join(search(route, p) for p in prefixes) + "]\n"
    configure(proj, build, text, **kw)
    return proj, build


def commands(build: Path) -> list[str]:
    """The build's commands, with the build directory made anonymous."""
    out = ninja(build, "-t", "commands")
    return [line.replace(str(build), "$B") for line in out.splitlines()]


def test_downstream_commands_equal_cps(tmp_path, pkgx, pkgy):
    """The same install consumed through CPS and through the `buildlog`
    format gives the same downstream commands, for a package with two
    components and for one that requires another package (pkgx through pkgy)."""
    for src, prefixes in [(PKGX, [pkgx[1]]), (PKGY, [pkgx[1], pkgy[1]])]:
        built = {}
        for route in ("cps", "buildlog"):
            name = f"{src.name}-{route}"
            _, build = downstream(tmp_path, name, src, route, prefixes)
            ninja(build)
            subprocess.run([str(build / "souffle" / "bin" / REL / "app")], check=True)
            built[route] = commands(build)
        assert built["cps"] == built["buildlog"], "\n".join(
            difflib.unified_diff(built["cps"], built["buildlog"], lineterm="", n=0)
        )
        assert any("-DA_API=1" in c or "-DY_API=5" in c for c in built["cps"])


def test_importer_rows_equal_cps(pkgx, pkgy):
    """Row for row, the buildlog importer states what the CPS importer does
    for the same install, except what only it has: the rule package, and
    attributes (native components' types and names)."""
    for pkg, (_, prefix) in {"pkgx": pkgx, "pkgy": pkgy}.items():
        cps = importing("cps", prefix / "share" / "cps" / f"{pkg}.cps")
        bl = importing("buildlog", facts_file(prefix, pkg))
        assert without(bl, BUILDLOG_ONLY) == cps
        manifest = importing("buildlog", prefix / "share" / "buildlog" / pkg / "manifest.toml")
        assert {k for k, _ in manifest} == {"import", "file", "rule_package"}


def test_prefix_search_binds_the_buildlog_route(tmp_path, pkgx):
    """One install writes a CPS file and a buildlog package; the CPS file names
    the manifest as its sibling (x_buildlog_manifest), so a plain prefix search
    sees one candidate for `pkgx:a` and binds the buildlog route: the package is
    pinned in the lock. Nothing is ambiguous and no format outranks another."""
    cps = json.loads((pkgx[1] / "share" / "cps" / "pkgx.cps").read_text())
    assert cps["x_buildlog_manifest"] == "share/buildlog/pkgx/manifest.toml"
    proj, build = downstream(tmp_path, "prefix", PKGX, "prefix", [pkgx[1]])
    assert "pkgx" in lock_of(proj)
    _, cps_build = downstream(tmp_path, "cps", PKGX, "cps", [pkgx[1]])
    assert commands(build) == commands(cps_build)


def marked_copy(tmp: Path, pkgx, name: str, marker) -> Path:
    """A copy of the pkgx install whose CPS file's marker is `marker` (None:
    removed, as in a CPS file buildlog did not write)."""
    prefix = tmp / name
    shutil.copytree(pkgx[1], prefix)
    path = prefix / "share" / "cps" / "pkgx.cps"
    doc = json.loads(path.read_text())
    if marker is None:
        del doc["x_buildlog_manifest"]
    else:
        doc["x_buildlog_manifest"] = marker
    path.write_text(json.dumps(doc, indent=2))
    return prefix


def test_an_unmarked_cps_file_and_a_manifest_are_ambiguous(tmp_path, pkgx, capsys):
    """A third-party CPS file (no marker) is not the manifest's package by
    identity: both define `pkgx:a`, so it is ambiguous until pinned, as for any
    two candidates."""
    prefix = marked_copy(tmp_path, pkgx, "unmarked", None)
    text = f'package_prefix = [["{prefix}"]]\n'
    with pytest.raises(SystemExit):
        user_configure(tmp_path, "unmarked", text, update_lock=True)
    assert "ambiguous package" in capsys.readouterr().err
    manifest = prefix / "share" / "buildlog" / "pkgx" / "manifest.toml"
    _, build = user_configure(
        tmp_path,
        "unmarked-pin",
        text + f'package_pin = [["pkgx:a", "{manifest}"]]\n',
        update_lock=True,
    )
    assert "-DA_API=1" in ninja(build, "-t", "commands")


@pytest.mark.parametrize(
    "marker, message",
    [
        ("share/buildlog/gone/manifest.toml", "names no manifest"),
        ("share/buildlog/other/manifest.toml", "names the manifest of 'other'"),
        ("../../elsewhere/manifest.toml", "leaves the prefix"),
        ("/abs/manifest.toml", "relative to the prefix"),
    ],
)
def test_a_bad_marker_is_an_error(tmp_path, pkgx, capsys, marker, message):
    """A marker that does not name an existing manifest of the same package in
    the same prefix is an error, never a silent fallback to the CPS file."""
    prefix = marked_copy(tmp_path, pkgx, "bad", marker)
    other = prefix / "share" / "buildlog" / "other"
    other.mkdir()
    (other / "manifest.toml").write_text('name = "other"\nversion = "1"\n')
    with pytest.raises(SystemExit):
        user_configure(tmp_path, "bad", f'package_prefix = [["{prefix}"]]\n', update_lock=True)
    assert message in capsys.readouterr().err


def lock_of(project: Path) -> dict[str, dict]:
    return {p["name"]: p for p in tomllib.loads((project / "buildlog.lock").read_text())["package"]}


def test_package_is_pinned_in_the_lock(tmp_path, pkgx):
    """The imported package is a rule package like any: the lock pins it by
    content hash, and changing the installed facts is an error until
    --update-lock (the loader's lock check applies unchanged)."""
    proj, _ = downstream(tmp_path, "lock", PKGX, "buildlog", [pkgx[1]])
    entry = lock_of(proj)["pkgx"]
    facts = facts_file(pkgx[1], "pkgx")
    assert entry["version"] == "1.2.3" and entry["source"] == "path"
    assert entry["hash"].startswith("sha256:")
    manifest = facts.with_name("manifest.toml")
    assert entry["reason"] == f"buildlog: {os.path.relpath(manifest, proj)}"
    # a relocated copy is the same package: the same hash
    moved = tmp_path / "moved"
    shutil.copytree(pkgx[1], moved)
    proj2, _ = downstream(tmp_path, "lock2", PKGX, "buildlog", [moved])
    assert lock_of(proj2)["pkgx"]["hash"] == entry["hash"]
    # a changed install is not: the lock check refuses it until --update-lock
    edited = tmp_path / "edited"
    shutil.copytree(pkgx[1], edited)
    f = facts_file(edited, "pkgx")
    f.write_text(f.read_text().replace("A_API=1", "A_API=9"))
    proj3 = copy_project(PKGX / "downstream", tmp_path / "proj3")
    shutil.copy(proj / "buildlog.lock", proj3 / "buildlog.lock")
    user = tmp_path / "u3.toml"
    user.write_text(f"package_search_dir = [{search('buildlog', edited)}]\n")
    with pytest.raises(LoaderError, match="pkgx hash changed"):
        configure_project(proj3, tmp_path / "b3", user_files=[PRESET, user])
    configure_project(proj3, tmp_path / "b3", user_files=[PRESET, user], update_lock=True)
    assert lock_of(proj3)["pkgx"]["hash"] != entry["hash"]


def rounds_outer(build: Path) -> int:
    return len(json.loads((build / "metrics.json").read_text())["outer"])


def test_a_locked_package_is_found_again_never_loaded_from_the_lock(tmp_path, pkgx):
    """The lock pins content, not usage: prepare and bind again without restart."""
    proj, build = downstream(tmp_path, "first", PKGX, "buildlog", [pkgx[1]])
    assert rounds_outer(build) == 1
    user = tmp_path / "first" / "b-user.toml"
    again = tmp_path / "again"
    configure_project(proj, again, user_files=[PRESET, user])
    assert rounds_outer(again) == 1
    assert "pkgx" in lock_of(proj)
    # the project now finds pkgx through CPS: the pinned package is not
    # discovered, so it is not kept
    cps_user = tmp_path / "cps-user.toml"
    cps_user.write_text(f"package_search_dir = [{search('cps', pkgx[1])}]\n")
    configure_project(proj, tmp_path / "cps", user_files=[PRESET, cps_user])
    assert "pkgx" not in lock_of(proj)


def test_why_names_the_installed_facts_file(tmp_path, pkgx, capsys):
    """`buildlog why` on an imported fact leads to the line of the installed
    package's facts file that states it."""
    proj, build = downstream(tmp_path, "why", PKGX, "buildlog", [pkgx[1]])
    facts = facts_file(pkgx[1], "pkgx")
    line = next(i for i, ln in enumerate(facts.read_text().splitlines(), 1) if "A_API=1" in ln)
    fact = 'prop_when("pkgx:a", "cc.define", "A_API=1", "interface", "on:arm64-macos")'
    assert cli.main(["why", "-C", str(build), fact, str(proj)]) == 0
    assert f"← {facts}:{line}" in capsys.readouterr().out


# ---- shipped rules -----------------------------------------------------------------


@pytest.fixture(scope="module")
def minidsl(tmp_path_factory):
    return install(DSL / "project", tmp_path_factory.mktemp("dsl"), "dsl")


def test_install_ships_the_rule_package(minidsl):
    """export_rules(pkg, dir): the project's own rule package is installed
    next to the facts, unchanged, and its manifest lists them."""
    _, prefix = minidsl
    pkgdir = prefix / "share" / "buildlog" / "minidsl"
    src = DSL / "project" / "packages" / "minidsl"
    assert (pkgdir / "L5_minidsl.dl").read_bytes() == (src / "L5_minidsl.dl").read_bytes()
    manifest = tomllib.loads((pkgdir / "manifest.toml").read_text())
    assert manifest["layers"] == ["L5_minidsl.dl"]
    assert manifest["imports"] == [{"format": "buildlog", "file": "minidsl.facts.dl"}]
    own = tomllib.loads((src / "manifest.toml").read_text())
    assert {k: v for k, v in manifest.items() if k != "imports"} == own
    assert (pkgdir / "minidsl.facts.dl").is_file()


def test_shipped_rule_builds_downstream(tmp_path, minidsl):
    """The installed rule translates the downstream's tables.dsl into a C++
    source, compiled into the app, which links against nothing but the
    runtime header minidsl installs. The facts alone (CPS) carry no rule: the
    .dsl source is then unclaimed. A plain prefix search binds the buildlog
    route (the CPS file names its manifest), so the rule applies by default."""
    proj, build = downstream(tmp_path, "dsl", DSL, "prefix", [minidsl[1]])
    out = ninja(build)
    assert "MINIDSL gen/" in out
    subprocess.run([str(build / "souffle" / "bin" / REL / "app")], check=True)
    generated = next((build / "souffle" / "gen").rglob("tables.dsl.cpp")).read_text()
    assert generated == "#include <minidsl/rt.h>\nMINIDSL_CONST(ANSWER, 42)\n"
    entry = lock_of(proj)["minidsl"]
    assert entry["hash"].startswith("sha256:") and entry["version"] == "1.0.0"
    assert lock_of(proj)["buildlog-cpp"]["reason"] == "uses"
    # via CPS: same facts, but no rules
    cproj = copy_project(DSL / "downstream", tmp_path / "proj-dsl-cps")
    user = tmp_path / "ucps.toml"
    user.write_text(f"package_search_dir = [{search('cps', minidsl[1])}]\n")
    with pytest.raises(SystemExit):
        configure_project(cproj, tmp_path / "b-dsl-cps", user_files=[PRESET, user])


def test_independent_generating_libraries_compose_with_a_dormant_package(tmp_path):
    example = runpy.run_path(str(ROOT / "examples/composition/run.py"))
    result = example["run"](tmp_path)
    assert result["output"] == "42"
    assert result["dormant_effects_absent"] and result["single_composition"]


def bad_rules(
    tmp_path: Path, name: str, layer: str, extends: tuple[str, ...] = ()
) -> tuple[Path, Path]:
    """A copy of the DSL project whose shipped layer is `layer`, installed;
    the prefix and a downstream project that finds it."""
    proj = copy_project(DSL / "project", tmp_path / f"src-{name}")
    pkg = proj / "packages" / "minidsl"
    (pkg / "L5_minidsl.dl").write_text(layer)
    manifest = pkg / "manifest.toml"
    manifest.write_text(
        manifest.read_text().replace(
            '  "target_action",\n', '  "target_action",\n' + "".join(f'  "{e}",\n' for e in extends)
        )
    )
    prefix, build = tmp_path / f"pfx-{name}", tmp_path / f"bb-{name}"
    configure(proj, build, f'install_prefix = "{prefix}"\n')
    ninja(build, "install")
    return prefix, copy_project(DSL / "downstream", tmp_path / f"down-{name}")


@pytest.mark.parametrize(
    ("layer", "extends", "message"),
    [
        # W5: what is requested depends on what discovery found
        (
            (
                ".decl minidsl_seen(p:symbol)\nminidsl_seen(p) :- package_of(_, _, p, _).\n"
                "request_all(p) :- minidsl_seen(p).\n"
            ),
            ("request_all",),
            "W5",
        ),
        # visibility: another package's private relation
        (
            ".decl minidsl_x(t:symbol)\nminidsl_x(t) :- import_candidate(t, _, _, _).\n",
            (),
            "private to buildlog",
        ),
        # layering: a relation the package does not extend
        ('target("minidsl:forged").\n', (), "not declared in extends"),
    ],
    ids=["W5", "visibility", "layering"],
)
def test_loader_checks_apply_to_shipped_rules(tmp_path, layer, extends, message):
    """A shipped rule is checked like any package's: W5 (decision relations
    may not depend on probe results), visibility of private names and the
    layering of extension points."""
    prefix, proj = bad_rules(tmp_path, "w", layer, extends)
    user = tmp_path / "u.toml"
    user.write_text(f"package_search_dir = [{search('buildlog', prefix)}]\n")
    with pytest.raises(LoaderError, match=message):
        configure_project(proj, tmp_path / "b", user_files=[PRESET, user])


def test_export_rules_errors(tmp_path):
    """export_rules must name a project-local rule package of the same name."""
    proj = copy_project(DSL / "project", tmp_path / "src")
    spec = proj / "spec.toml"
    original = spec.read_text()
    spec.write_text(original.replace("src:packages/minidsl", "src:packages/nothere"))
    with pytest.raises(SpecError, match="no manifest.toml"):
        configure_project(proj, tmp_path / "b1", user_files=[PRESET], emit=False)
    spec.write_text(original.replace('["minidsl", "src', '["other", "src'))
    with pytest.raises(SpecError, match="it ships as 'other'"):
        configure_project(proj, tmp_path / "b2", user_files=[PRESET], emit=False)


# ---- native components ---------------------------------------------------------------


@pytest.mark.parametrize("linkage", ["shared", "static"])
def test_native_component_facts_agree_with_cps(tmp_path, linkage):
    """A library component's artifact is stated by the same facts the other
    formats are written from: its type, its prefix-relative location, and for
    a shared library its install name; and the importer states them as
    attributes of the target."""
    from tests import host
    from tests.test_cpp_mechanisms import project as native_project
    from tests.test_export_native import FILES, SPEC
    from tests.test_export_native import configure as native_configure

    spec = SPEC.replace(
        'default_linkage = [["foo", "shared"]]', f'default_linkage = [["foo", "{linkage}"]]'
    )
    proj = native_project(tmp_path, spec, FILES)
    prefix = tmp_path / "prefix"
    build, metrics = native_configure(proj, tmp_path, prefix=prefix)
    assert metrics["errors"] == []
    ninja(build, "install")
    file = facts_file(prefix, "Foo")
    rows = {(rel, row) for rel, row, _ in dl_package.parse_facts(file.read_text())}
    cps = json.loads((prefix / "lib" / "cps" / "Foo.cps").read_text())["components"]["foo"]
    assert cps["location"].replace("@prefix@/", "") == next(
        r[1] for k, r in rows if k == "pkg_location"
    )
    kind = "dylib" if linkage == "shared" else "archive"
    assert ("pkg_component", ("foo", kind)) in rows and kind == cps["type"]
    assert ("pkg_link_name", ("foo", "foo")) in rows
    install_name = ("@rpath/" if host.MACOS else "") + host.shlib("foo", "1")
    soname = {r[1] for k, r in rows if k == "pkg_soname"}
    assert soname == ({install_name} if linkage == "shared" else set())
    imported = importing("buildlog", file)
    assert ("target", ("Foo:foo", "library")) in imported
    attrs = {(row[1], row[2]) for k, row in imported if k == "attr" and row[0] == "Foo:foo"}
    assert ("type", kind) in attrs and ("link_name", "foo") in attrs
    assert (("soname", install_name) in attrs) == (linkage == "shared")
    where = next(row for k, row in imported if k == "location" and row[0] == "Foo:foo")
    assert where[1] == str(prefix / cps["location"].replace("@prefix@/", ""))


def test_the_format_can_be_skipped(tmp_path):
    """export_format_skip names the format like any other: no package is written."""
    proj = copy_project(PKGX / "project", tmp_path / "src")
    prefix, build = tmp_path / "pfx", tmp_path / "b"
    configure(
        proj, build, f'install_prefix = "{prefix}"\nexport_format_skip = [["pkgx", "buildlog"]]\n'
    )
    ninja(build, "install")
    assert (prefix / "share" / "cps" / "pkgx.cps").is_file()
    assert not (prefix / "share" / "buildlog").exists()


# ---- a manifest that imports other formats --------------------------------------------

IMPORTS = FIXTURES / "export-imports"


def hand_written_upstream(tmp: Path) -> Path:
    """An install with only a CMake export and a hand-written manifest that
    imports it; the shipped layer is the minidsl rule."""
    prefix = tmp / "prefix"
    shutil.copytree(IMPORTS / "prefix", prefix)
    layer = DSL / "project" / "packages" / "minidsl" / "L5_minidsl.dl"
    # the CMake export's target keeps the id it was exported under
    text = layer.read_text().replace('"minidsl:rt"', '"minidsl::rt"')
    (prefix / "share" / "buildlog" / "minidsl" / layer.name).write_text(text)
    return prefix


def test_manifest_imports_a_cmake_export_and_ships_rules(tmp_path):
    """The manifest is the entry point: the core hands its CMake export to the
    cmake provider (an included file), the targets come from there, the
    package's rules are loaded and pinned, and the lock's hash covers the
    imported file."""
    prefix = hand_written_upstream(tmp_path)
    proj = copy_project(DSL / "downstream", tmp_path / "proj")
    user = tmp_path / "u.toml"
    user.write_text(f"package_search_dir = [{search('buildlog', prefix)}]\n")
    build = tmp_path / "b"
    configure_project(proj, build, user_files=[PRESET, user], update_lock=True)
    ninja(build)
    assert (
        subprocess.run([str(build / "souffle" / "bin" / REL / "app")], check=False).returncode == 0
    )
    cmds = ninja(build, "-t", "commands")
    assert f"-isystem {prefix}/include" in cmds or f"-I{prefix}/include" in cmds
    assert "-DMINIDSL=1" in cmds
    entry = lock_of(proj)["minidsl"]
    cmake = prefix / "lib" / "cmake" / "minidsl" / "minidslTargets.cmake"
    cmake.write_text(cmake.read_text().replace("MINIDSL=1", "MINIDSL=2"))
    with pytest.raises(LoaderError, match="minidsl hash changed"):
        configure_project(proj, tmp_path / "b2", user_files=[PRESET, user])
    configure_project(proj, tmp_path / "b2", user_files=[PRESET, user], update_lock=True)
    assert lock_of(proj)["minidsl"]["hash"] != entry["hash"]


@pytest.mark.parametrize(
    "imports",
    [
        '[{ format = "cmake", file = "../../../../etc/x.cmake" }]',
        '[{ format = "cmake", file = "lib/../../../../../x.cmake" }]',
        '[{ format = "cmake", file = "/abs/x.cmake" }]',
        '[{ format = "cmake" }]',
        '[{ format = "cmake", file = "x", other = "y" }]',
    ],
)
def test_manifest_imports_must_stay_in_the_prefix(tmp_path, imports):
    prefix = hand_written_upstream(tmp_path)
    manifest = prefix / "share" / "buildlog" / "minidsl" / "manifest.toml"
    text = manifest.read_text()
    old = next(line for line in text.splitlines() if line.startswith("imports"))
    manifest.write_text(text.replace(old, f"imports = {imports}"))
    proj = copy_project(DSL / "downstream", tmp_path / "proj")
    user = tmp_path / "u.toml"
    user.write_text(f"package_search_dir = [{search('buildlog', prefix)}]\n")
    with pytest.raises(LoaderError, match="imports"):
        configure_project(proj, tmp_path / "b", user_files=[PRESET, user], update_lock=True)


# ---- a package that imports files asserts only about its own namespaces ---------------


def with_rules(tmp: Path, rules: str, facts: str = "") -> tuple[Path, Path, Path]:
    """hand_written_upstream plus `rules` in the shipped layer (the manifest
    extends what they need), and a `facts` TOML file when given; the prefix,
    a downstream project and a user file that searches the prefix."""
    prefix = hand_written_upstream(tmp)
    pkg = prefix / "share" / "buildlog" / "minidsl"
    layer = pkg / "L5_minidsl.dl"
    layer.write_text(layer.read_text() + rules)
    manifest = pkg / "manifest.toml"
    text = manifest.read_text().replace(
        '  "target_action",\n',
        '  "target_action",\n  "target",\n  "prop",\n  "dep",\n  "package_attr",\n',
    )
    if facts:
        (pkg / "facts.toml").write_text(facts)
        text = text.replace(
            'layers = ["L5_minidsl.dl"]', 'layers = ["L5_minidsl.dl"]\nfacts = ["facts.toml"]'
        )
    manifest.write_text(text)
    user = tmp / "u.toml"
    user.write_text(f"package_search_dir = [{search('buildlog', prefix)}]\n")
    return prefix, copy_project(DSL / "downstream", tmp / "proj"), user


@pytest.mark.parametrize(
    "rule",
    [
        'prop("other::x", "cc.define", "A", "interface").',
        'prop(cat("other::", "x"), "cc.define", "A", "interface").',
        'target("minidsl").',
        'dep(t, "minidsl::rt", "public") :- target_sources(t, _).',
        'package_attr("other", "p", "k", "v").',
    ],
    ids=["literal", "cat", "bare-id", "unbound-variable", "package"],
)
def test_a_package_asserts_only_about_the_namespaces_it_imports(tmp_path, rule):
    """A package that imports files may state target facts only for targets of
    its own namespaces (its name, its target_namespace, its imports'): a
    literal or cat() id in one, or a variable bound by import_ns_target to
    one. A layer asserting about another package's targets, or about any
    target, is a load error."""
    _, proj, user = with_rules(tmp_path, rule + "\n")
    with pytest.raises(LoaderError, match="outside the namespaces its imports own"):
        configure_project(proj, tmp_path / "b", user_files=[PRESET, user], update_lock=True)


def test_own_namespace_assertions_are_accepted(tmp_path):
    rules = (
        'prop(t, "cc.define", "OWN=1", "interface") :- import_ns_target("minidsl", t).\n'
        'prop("minidsl::rt", "cc.define", "OWN=2", "interface").\n'
    )
    _, proj, user = with_rules(tmp_path, rules)
    build = tmp_path / "b"
    configure_project(proj, build, user_files=[PRESET, user], update_lock=True)
    assert "-DOWN=2" in ninja(build, "-t", "commands")


def test_a_package_property_that_contradicts_its_facts_is_an_error(tmp_path, capsys):
    """The package's facts file and its rules state one single-valued property
    (cxx.rtti) differently: buildlog-cpp's C++ ABI relation reports it, whatever
    the source of either value; neither wins."""
    facts = 'prop = [["minidsl::rt", "cxx.rtti", "on", "private"]]\n'
    _, proj, user = with_rules(
        tmp_path, 'prop("minidsl::rt", "cxx.rtti", "off", "private").\n', facts
    )
    with pytest.raises(SystemExit):
        configure_project(proj, tmp_path / "b", user_files=[PRESET, user], update_lock=True)
    assert "conflicting C++ ABI settings" in capsys.readouterr().err


# ---- selection stays in the core: no first match -----------------------------------------


def two_prefixes(tmp: Path, pkgx) -> tuple[Path, Path]:
    """Two installs of pkgx, one of them with a different define."""
    a, b = tmp / "a", tmp / "b"
    shutil.copytree(pkgx[1], a)
    shutil.copytree(pkgx[1], b)
    f = facts_file(b, "pkgx")
    f.write_text(f.read_text().replace("A_API=1", "A_API=7"))
    return a, b


def user_configure(tmp: Path, name: str, text: str, **kw) -> tuple[Path, Path]:
    proj = copy_project(PKGX / "downstream", tmp / f"proj-{name}")
    user = tmp / f"{name}.toml"
    user.write_text(text)
    build = tmp / f"b-{name}"
    configure_project(proj, build, user_files=[PRESET, user], **kw)
    return proj, build


def test_every_candidate_is_found_and_ambiguity_is_an_error(tmp_path, pkgx, capsys):
    """The provider enumerates every candidate under every search directory;
    the core selects. Two prefixes that both hold the package are an
    `ambiguous package` error, whatever their order, until a package_pin names
    one manifest; the pinned one is the one bound."""
    a, b = two_prefixes(tmp_path, pkgx)
    for i, order in enumerate(([a, b], [b, a])):
        dirs = ", ".join(search("buildlog", p) for p in order)
        with pytest.raises(SystemExit):
            user_configure(
                tmp_path, f"amb{i}", f"package_search_dir = [{dirs}]\n", update_lock=True
            )
        err = capsys.readouterr().err
        assert "ambiguous package" in err and str(a) in err and str(b) in err
    dirs = ", ".join(search("buildlog", p) for p in (a, b))
    for chosen, define in ((a, "A_API=1"), (b, "A_API=7")):
        manifest = chosen / "share" / "buildlog" / "pkgx" / "manifest.toml"
        pin = f'package_pin = [["pkgx:a", "{manifest}"]]\n'
        _, build = user_configure(
            tmp_path,
            f"pin-{chosen.name}",
            f"package_search_dir = [{dirs}]\n{pin}",
            update_lock=True,
        )
        assert f"-D{define}" in ninja(build, "-t", "commands")


def test_a_resolver_can_name_the_exact_manifest(tmp_path, pkgx):
    """No search: package_file imports one named manifest outright; a prefix
    search binds the buildlog route (one candidate); and a pin can name the CPS
    file instead, even where the manifest is the same package."""
    _, b = two_prefixes(tmp_path, pkgx)
    manifest = b / "share" / "buildlog" / "pkgx" / "manifest.toml"
    _, build = user_configure(
        tmp_path, "file", f'package_file = [["buildlog", "{manifest}"]]\n', update_lock=True
    )
    assert "-DA_API=7" in ninja(build, "-t", "commands")
    prefix = f'package_prefix = [["{b}"]]\n'
    _, build = user_configure(tmp_path, "prefix", prefix, update_lock=True)
    assert "-DA_API=7" in ninja(build, "-t", "commands")
    cps = b / "share" / "cps" / "pkgx.cps"
    pin = f'package_pin = [["pkgx:a", "{cps}"]]\n'
    _, build = user_configure(tmp_path, "cps", prefix + pin, update_lock=True)
    assert "-DA_API=1" in ninja(build, "-t", "commands")


# ---- shipped rules are a loss in every foreign format --------------------------------

RULES_KIND = "export format cannot carry shipped rules"


def rules_warnings(metrics: dict) -> list[str]:
    return sorted(d for k, d in metrics["warnings"] if k == RULES_KIND)


def test_foreign_formats_warn_that_they_drop_the_rules(tmp_path):
    """A package that ships rules warns once per foreign format (cmake, cps,
    pc), naming the package, its rule directory and the format; the buildlog
    format does not warn. It is a warning, not an error."""
    metrics = configure(copy_project(DSL / "project", tmp_path / "src"), tmp_path / "b", "")
    got = rules_warnings(metrics)
    assert [w.split(" are not in ")[1].split(" ")[0] for w in got] == ["cmake", "cps", "pc"]
    assert all(w.startswith("minidsl: the rules of src:packages/minidsl are not in ") for w in got)
    assert all("only the buildlog format ships them" in w for w in got)
    lossy = (tmp_path / "b" / "souffle" / "facts" / "export_rules_lossy.csv").read_text()
    assert sorted(r.split("\t")[1] for r in lossy.splitlines()) == ["cmake", "cps", "pc"]
    assert not any("buildlog" in r.split("\t")[1] for r in lossy.splitlines())


def test_skipping_a_format_silences_its_rules_warning(tmp_path):
    metrics = configure(
        copy_project(DSL / "project", tmp_path / "src"),
        tmp_path / "b",
        'export_format_skip = [["minidsl", "cmake"], ["minidsl", "pc"]]\n',
    )
    ((_, detail),) = [w for w in metrics["warnings"] if w[0] == RULES_KIND]
    assert " are not in cps " in detail


def test_rule_free_package_does_not_warn(tmp_path):
    proj = copy_project(DSL / "project", tmp_path / "src")
    spec = proj / "spec.toml"
    spec.write_text(
        "".join(l for l in spec.read_text().splitlines(True) if not l.startswith("export_rules"))
    )
    assert rules_warnings(configure(proj, tmp_path / "b", "")) == []


# ---- lock seeding and discovery termination -------------------------------------------


def self_wanting(tmp_path: Path):
    """A prefix whose minidsl rules ask for minidsl itself (package_want of an
    id the package owns), and a downstream that depends on it, configured once
    so that the lock pins minidsl; then the downstream drops the dependency.
    Returns (project without the dependency, user file, lock text)."""
    shipped = (DSL / "project" / "packages" / "minidsl" / "L5_minidsl.dl").read_text()
    prefix, down = bad_rules(
        tmp_path, "self", shipped + 'package_want("minidsl:rt").\n', ("package_want",)
    )
    user = tmp_path / "u.toml"
    user.write_text(f"package_search_dir = [{search('buildlog', prefix)}]\n")
    configure_project(down, tmp_path / "b0", user_files=[PRESET, user])
    assert "minidsl" in lock_of(down)
    spec = down / "spec.toml"
    spec.write_text(
        spec.read_text()
        .replace('dep = { "minidsl:rt" = "private" }\n', "")
        .replace(', "tables.dsl"', "")
    )
    return down, user


def test_a_lock_cannot_justify_a_package_by_its_own_rules(tmp_path):
    """minidsl's own rules ask for minidsl; nothing else does. A lock that
    loaded it would make discovery find it again only because it was loaded, so
    the result would depend on the lock. The run without a lock and the run with one must agree:
    minidsl is not loaded (the lock never loads packages)."""
    down, user = self_wanting(tmp_path)
    locked = tmp_path / "locked.txt"
    locked.write_text((down / "buildlog.lock").read_text())
    (down / "buildlog.lock").unlink()
    _, fresh = configure_project(down, tmp_path / "b1", user_files=[PRESET, user])
    assert "minidsl" not in lock_of(down)
    (down / "buildlog.lock").write_text(locked.read_text())
    _, seeded = configure_project(down, tmp_path / "b2", user_files=[PRESET, user])
    assert "minidsl" not in lock_of(down)
    assert seeded["outer"][-1]["packages"] == fresh["outer"][-1]["packages"]


def test_a_recorded_self_requesting_package_is_not_activated_by_the_state(tmp_path):
    """Available code cannot activate itself through a use-guarded request."""
    down, user = self_wanting(tmp_path)
    shutil.copytree(tmp_path / "b0", tmp_path / "b1")
    state = json.loads((tmp_path / "b0" / "buildlog-state.json").read_text())
    assert [Path(p).name for p, _ in state["discovered"]] == ["minidsl"]
    _, hinted = configure_project(down, tmp_path / "b0", user_files=[PRESET, user])
    assert "minidsl" not in lock_of(down)
    assert hinted["composition_calls"] == 1
    assert "minidsl" in hinted["outer"][0]["packages"]
    _, plain = configure_project(down, tmp_path / "b1", user_files=[PRESET, user])
    assert plain["outer"][-1]["packages"] == hinted["outer"][-1]["packages"]
    assert json.loads((tmp_path / "b0" / "buildlog-state.json").read_text())["discovered"] == []


def test_a_self_requesting_package_stays_when_something_else_asks(tmp_path):
    """The same package, still depended on: the lock does not seed it (its
    rules state a relation discovery depends on), discovery finds it again from
    the downstream's dependency, and the lock keeps it."""
    shipped = (DSL / "project" / "packages" / "minidsl" / "L5_minidsl.dl").read_text()
    prefix, down = bad_rules(
        tmp_path, "self2", shipped + 'package_want("minidsl:rt").\n', ("package_want",)
    )
    user = tmp_path / "u2.toml"
    user.write_text(f"package_search_dir = [{search('buildlog', prefix)}]\n")
    for run in ("cold", "locked"):
        _, metrics = configure_project(down, tmp_path / f"b-{run}", user_files=[PRESET, user])
        assert "minidsl" in metrics["outer"][-1]["packages"]
        assert "minidsl" in lock_of(down)
    assert metrics["composition_calls"] == 1
