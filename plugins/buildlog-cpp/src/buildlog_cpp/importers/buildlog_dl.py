# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The `buildlog` format importer: a package installed as a rule package.

The entry point is the package's manifest, `<prefix>/share/buildlog/<Pkg>/
manifest.toml`. Its `imports` name nearby export files (paths relative to the
manifest, inside the prefix), each with the format whose provider reads it:
the core hands the files over as included rows, as for any file that includes
another. buildlog's own install lists one, `{ format = "buildlog", file =
"<Pkg>.facts.dl" }`, the facts it states in the `pkg_*` vocabulary
(`buildlog.dl_package`: ground Soufflé facts); an upstream with only a CMake
export lists that, with the `namespace` its targets are imported under.
The manifest import also names the package's directory (`rule_package`): the
outer loop loads it, pins it in the lock by content hash (the imported files
included) and applies the loader's checks to its rules.

The facts file states the same rows as every other importer. Targets are
`<Pkg>:<component>` and `<Pkg>:*` (the default components), the ids CPS has,
so a downstream names a package's components the same whichever route binds
them. A prefix that holds this package's CPS file too has one candidate,
not two: buildlog's own CPS export names the manifest as its sibling
(`x_buildlog_manifest`, see cps.same_as) and the core binds the manifest. A
third-party CPS file without that marker makes the name ambiguous until
pinned. Nothing is translated: the file's keys are buildlog's own, its paths are prefix-relative
(`pkg_prefix` finds the prefix from the file's location), and a requirement on
another package is an edge to its `<Other>:<component>`.

Not imported: `pkg_compat`, `pkg_description` and `pkg_requires_version`
describe the package, they are not usage requirements (CPS's importer skips
`compat_version`, `description` and `requires` versions too).
"""

from __future__ import annotations

import os
from pathlib import Path

from buildlog import dl_package, providers
from buildlog.layers import parse_manifest
from buildlog.providers import MetadataLinks

from .common import CPS_DEFAULT, TRUE, MetadataError, Result, cps_id, exact, stat_token
from .cps import split_name

SUFFIX = dl_package.FACTS_SUFFIX
MANIFEST = "manifest.toml"
LIBRARY = {"archive", "dylib"}


def by_alias(name: str) -> bool:
    """The manifest defines its ids like any provider's file."""
    return False


def _candidate(pkg: str, directory: Path) -> Path:
    return directory / pkg / MANIFEST


def _read(path: Path) -> list[tuple[str, tuple[str, ...], int]]:
    try:
        return dl_package.parse_facts(path.read_text())
    except dl_package.FactsError as exc:
        raise MetadataError(exc.line, exc.message) from exc


def _manifest(path: Path) -> dict:
    errors: list[str] = []
    data = parse_manifest(path, errors)
    if errors:
        raise MetadataError(None, "; ".join(errors))
    return data


def _facts_import(path: Path, data: dict) -> Path | None:
    """The package's own facts file: its `buildlog`-format import."""
    for i in data["imports"]:
        if i["format"] == "buildlog":
            return path.parent / i["file"]
    return None


def find(name: str, directory: Path) -> list[Path]:
    pkg = name.partition("::")[0] if "::" in name else (split_name(name) or ("", ""))[0]
    if not pkg or "/" in pkg or pkg.startswith(".") or name.count(":") > 2:
        return []
    path = _candidate(pkg, directory)
    if not path.is_file() or not exact(path) or not exact(path.parent):
        return []
    try:
        facts = _facts_import(path, _manifest(path))
        rows = _read(facts) if facts is not None else None
    except (MetadataError, OSError):
        return [path]  # the import reports it
    if rows is None:
        # Names come from imported files of other formats: any name in the
        # package's own namespace may be defined there.
        return [path] if ":" in name else []
    comp = split_name(name)
    if comp is None:
        return []
    if ":" not in name:
        return [path] if any(r == "pkg_default" for r, _, _ in rows) else []
    if comp[1] == CPS_DEFAULT or any(r == "pkg_component" and v[0] == comp[1] for r, v, _ in rows):
        return [path]
    return []


def offers(file: Path, directory: Path, names, observations) -> set[str]:
    if file.name != MANIFEST or file.parent.parent != directory:
        return set()
    package = file.parent.name
    return {name for name in {*names, package, f"{package}:*"} if file in find(name, directory)}


def enumerate_files(directory: Path, observations) -> list[Path]:
    files = []
    for child in observations.directory(directory):
        for path in observations.directory(child):
            if path.name == MANIFEST:
                files.append(path)
    return files


def metadata_links(file: Path, observations) -> MetadataLinks:
    observations.file(file)
    if file.name != MANIFEST:
        return MetadataLinks()
    rules = (file.parent,)
    try:
        data = _manifest(file)
    except (MetadataError, OSError) as exc:
        return MetadataLinks(rules=rules, error=str(exc))
    return MetadataLinks(
        includes=tuple(
            (i["format"], file.parent / i["file"], i["namespace"]) for i in data["imports"]
        ),
        rules=rules,
    )


def find_watched(name: str, directory: Path) -> list:
    pkg = name.partition(":")[0]
    d = directory / pkg
    manifest = _candidate(pkg, directory)
    try:
        facts = _facts_import(manifest, _manifest(manifest))
    except (MetadataError, OSError):
        facts = None
    return [
        stat_token(manifest),
        stat_token(facts) if facts is not None else None,
        [str(d), sorted(os.listdir(d)) if d.is_dir() else None],
    ]


def watched(path: Path) -> list:
    return [stat_token(path)]


def import_file(res: Result, results=None) -> None:
    """The manifest includes the files it imports, each for the provider of
    its format (the core hands them over), and names its directory as a rule
    package; a `.facts.dl` file is the format's own import."""
    if res.file.name != MANIFEST:
        _import_facts(res)
        return
    data = _manifest(res.file)
    pkg = res.file.parent.name
    if data["name"] != pkg:
        raise MetadataError(None, f"package {data['name']!r} is not named like its directory")
    for i in data["imports"]:
        if not providers.registered(i["format"]):
            raise MetadataError(None, f"imports names the unknown format {i['format']!r}")
        path = os.path.normpath(os.path.join(os.path.abspath(res.file.parent), i["file"]))
        res.include(i["format"], path, i["namespace"], None)
    res.rule_package(str(Path(os.path.abspath(res.file)).parent), None)


def _import_facts(res: Result) -> None:
    rows = _read(res.file)
    pkg_rows = [(v[0], n) for r, v, n in rows if r == "pkg_package"]
    if len(pkg_rows) != 1:
        raise MetadataError(None, "a package file states exactly one pkg_package")
    pkg, pline = pkg_rows[0]
    if res.file.name != f"{pkg}{SUFFIX}":
        raise MetadataError(pline, f"package {pkg!r} is not named like its file")
    if ":" in pkg:
        raise MetadataError(pline, f"package name {pkg!r} is malformed")
    by: dict[str, list[tuple[tuple[str, ...], int]]] = {}
    for rel, row, line in rows:
        by.setdefault(rel, []).append((row, line))

    comps: dict[str, tuple[str, int]] = {}
    for (comp, kind), line in by.get("pkg_component", []):
        if kind not in dl_package.KINDS:
            raise MetadataError(line, f"component {comp} has unknown kind {kind!r}")
        if comp in comps or comp == CPS_DEFAULT or ":" in comp or not comp:
            raise MetadataError(line, f"component name {comp!r} is not allowed or repeated")
        comps[comp] = (kind, line)
    for rel in (
        "pkg_default",
        "pkg_location",
        "pkg_link_name",
        "pkg_soname",
        "pkg_requires",
        "pkg_depends",
        "pkg_runtime_dep",
        "pkg_prop",
        "pkg_prop_path",
        "pkg_prop_abs",
    ):
        for row, line in by.get(rel, []):
            if row[0] not in comps:
                raise MetadataError(line, f"{rel} of the undeclared component {row[0]}")
    for (_, other), line in by.get("pkg_requires", []):
        if other not in comps:
            raise MetadataError(line, f"requirement on the undeclared component {other}")
    for (_, other, place), line in by.get("pkg_runtime_dep", []):
        if other not in comps or comps[other][0] != "module":
            raise MetadataError(
                line, f"run-time dependency on {other}, which is not a module component"
            )

    prefix = None
    if by.get("pkg_prefix"):
        (rel,), line = by["pkg_prefix"][0]
        if len(by["pkg_prefix"]) > 1:
            raise MetadataError(line, "pkg_prefix is stated twice")
        prefix = os.path.normpath(os.path.join(os.path.abspath(res.file.parent), rel))

    def under(rel: str, line: int) -> str:
        if prefix is None:
            raise MetadataError(
                line, "a prefix-relative path, but the package states no pkg_prefix"
            )
        if os.path.isabs(rel):
            raise MetadataError(line, f"{rel!r} must be relative to the prefix")
        return os.path.normpath(os.path.join(prefix, rel))

    version = by["pkg_version"][0][0][1] if by.get("pkg_version") else None
    vline = by["pkg_version"][0][1] if by.get("pkg_version") else None

    for comp, (kind, line) in comps.items():
        t = cps_id(pkg, comp)
        res.target(t, "library" if kind in LIBRARY else kind, line)
        res.package(t, pkg, comp, pline)
        if version:
            res.version_string(t, version, vline)
        if kind in LIBRARY:
            res.attr(t, "type", kind, line)
    for (comp, rel), line in by.get("pkg_location", []):
        res.location(cps_id(pkg, comp), under(rel, line), TRUE, line)
    for (comp, kind), line in by.get("pkg_component", []):
        if kind == "module":
            locs = [r[1] for r, _ in by.get("pkg_location", []) if r[0] == comp]
            if len(locs) != 1:
                raise MetadataError(line, f"module {comp} needs exactly one pkg_location")
            res.attr(cps_id(pkg, comp), "file", os.path.basename(locs[0]), line)
    for (comp, name), line in by.get("pkg_link_name", []):
        res.attr(cps_id(pkg, comp), "link_name", name, line)
    for (comp, name), line in by.get("pkg_soname", []):
        res.attr(cps_id(pkg, comp), "soname", name, line)
    for (comp, other), line in by.get("pkg_requires", []):
        res.dep(cps_id(pkg, comp), cps_id(pkg, other), "public", TRUE, line)
    for (comp, other, place), line in by.get("pkg_runtime_dep", []):
        res.runtime_dep(cps_id(pkg, comp), cps_id(pkg, other), place, TRUE, line)
    for (comp, dpkg, dcomp), line in by.get("pkg_depends", []):
        res.dep(cps_id(pkg, comp), cps_id(dpkg, dcomp or CPS_DEFAULT), "public", TRUE, line)
    for (comp, key, value), line in by.get("pkg_prop", []):
        res.prop(cps_id(pkg, comp), key, value, TRUE, line)
    for (comp, key, value), line in by.get("pkg_prop_abs", []):
        res.prop(cps_id(pkg, comp), key, value, TRUE, line)
    for (comp, key, rel), line in by.get("pkg_prop_path", []):
        res.prop(cps_id(pkg, comp), key, under(rel, line), TRUE, line)

    defaults = by.get("pkg_default", [])
    if defaults:
        default = cps_id(pkg, CPS_DEFAULT)
        dline = defaults[0][1]
        res.target(default, "interface", dline)
        res.package(default, pkg, "", pline)
        if version:
            res.version_string(default, version, vline)
        res.alias(pkg, default, dline)
        for (comp,), line in defaults:
            res.dep(default, cps_id(pkg, comp), "public", TRUE, line)
