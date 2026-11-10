# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Layer loader: manifests, package qualification, checks and generated IO.

The loader owns the program Soufflé sees. It package-qualifies private names,
checks every layer on Soufflé's `--show=initial-ast` of the qualified files,
and generates all `.input`/`.output`
directives from manifests. Layer files carry no IO.

Visibility: a package's `open` relations are readable
and extendable by higher layers, its `exports` (relations or types) are
readable only, and every other name is private. Private names are qualified
as `<package>__<name>` in every package, the core included, and the checks
reject references to another package's private names.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import posixpath
import re
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from . import compose_cache, souffle_cache
from .errors import BuildlogError, read_toml

if TYPE_CHECKING:
    from .modules import ScopedModules

FORBIDDEN = (".input", ".output", ".printsize", ".functor")
BUILTINS = {"cat", "substr", "count", "min", "max"}
PRIMITIVE_TYPES = {"symbol", "number", "unsigned", "float"}
NAME = r"[A-Za-z_][\w.]*"
# Hyphens are allowed (`buildlog-cpp`); a doubled separator is not, so the
# qualification prefix `name.replace("-", "_") + "__"` stays unambiguous.
PACKAGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*\Z")
NAMESPACE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
# Qualifier of the package's target ids (<q>::name); defaults to
# the package name. Unlike relation namespaces it may be capitalized (a CMake-style `Pkg`).
TARGET_NAMESPACE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*\Z")

LIST_KEYS = (
    "layers",
    "facts",
    "extends",
    "open",
    "exports",
    "accepts",
    "user",
    "driver",
    "outputs",
    "structural",
    "fold",
    "fold_origins",
    "elide",
    "flatten",
    "demand",
    "legend",
    "registered_includes",
)
MANIFEST_KEYS = {
    "name",
    "version",
    "rank",
    "namespace",
    "target_namespace",
    "requires",
    "probe",
    "provider",
    "stratum",
    "imports",
    *LIST_KEYS,
}
# Where a manifest came from. Only the core and registered Python plugins
# (tier 1) may declare probes; path-found rule packages (tier 2) are pure.
SOURCES = ("core", "plugin", "path")
PROBE_REQUIRED = ("kind", "request", "domain", "result", "entry")
# `fingerprint` is the probe's cache key: `module:function(args, results,
# **fingerprint_params)` returns a string that must change whenever anything
# the probe reads changes (files by identity, environment variables,
# earlier answers). A kind with NO fingerprint is uncached: never cached or
# seeded, it re-runs every configure. Completeness of a fingerprint is the
# plugin author's responsibility; it cannot be checked. `unguarded` is
# orthogonal: it exempts a (fingerprinted) kind from guarded seeding.
PROBE_KEYS = {*PROBE_REQUIRED, "fingerprint", "fingerprint_params", "unguarded"}
# A package provider: the format-owning half of core
# package discovery, a Python object named by `entry`, with an optional rule
# package (`rules`, a directory relative to the manifest) that is loaded
# when a discovery request first reaches the provider.
PROVIDER_REQUIRED = ("name", "entry")
PROVIDER_KEYS = {*PROVIDER_REQUIRED, "rules"}
PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")
# A build-time stratum: a Datalog program a build action runs (`buildlog -E
# collate package:name`), not part of the configure program. It reads facts
# the build produces (scan results); nothing configure-time may read what it
# declares (_check_strata). `entry` is the Python driver `module:function`.
STRATUM_KEYS = {"name", "program", "entry"}


@dataclass
class ProgramInfo:
    decls: dict[str, tuple[str, ...]] = field(default_factory=dict)
    types: set[str] = field(default_factory=set)
    inputs: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    directives: list[str] = field(default_factory=list)
    clauses: list[tuple[str, set[str], str]] = field(default_factory=list)


@dataclass(frozen=True)
class ProbeDecl:
    kind: str
    request: str
    domain: str
    results: tuple[str, ...]
    entry: str
    fingerprint: str | None
    fingerprint_params: tuple[tuple[str, object], ...]
    layer: str
    manifest: Path
    # Why rules may read this probe's results without its request (see
    # _check_answer_reads); such results are not seeded speculatively.
    unguarded: str | None = None


@dataclass(frozen=True)
class ProviderDecl:
    name: str
    entry: str
    rules: Path | None
    layer: str
    manifest: Path


@dataclass(frozen=True)
class StratumDecl:
    name: str
    program: Path
    entry: str
    layer: str
    manifest: Path


@dataclass(frozen=True)
class PackageRef:
    """A manifest to load and where it was found (one of SOURCES)."""

    manifest: Path
    source: str = "path"

    @property
    def tier(self) -> int:
        return 2 if self.source in {"path", "project"} else 1


@dataclass
class Layer:
    name: str
    rank: int
    version: str | None
    source: str
    namespace: str | None
    target_namespace: str
    manifest: Path
    files: list[Path]
    fact_files: list[Path]
    requires: dict[str, str]
    extends: set[str]
    open: set[str]
    exports: set[str]
    accepts: set[str]
    user: set[str]
    driver: set[str]
    outputs: set[str]
    registered_includes: set[str]
    probes: list[ProbeDecl]
    providers: list[ProviderDecl]
    qualified: dict[str, str]
    readable: set[str]
    program_files: list[Path]
    info: ProgramInfo
    text: str
    branches: dict[str, set[str]] = field(default_factory=dict)
    imports: list[dict] = field(default_factory=list)
    strata: list[StratumDecl] = field(default_factory=list)
    expanded: str = ""

    @property
    def tier(self) -> int:
        return 2 if self.source == "path" else 1

    @property
    def raw_decls(self) -> set[str]:
        """Relation names as written in the layer files (before qualification)."""
        unq = {q: raw for raw, q in self.qualified.items()}
        return {unq.get(rel, rel) for rel in self.info.decls}


@dataclass
class Program:
    build: Path | None
    layers: list[Layer]
    errors: list[str]
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    driver_inputs: list[str] = field(default_factory=list)
    # Restored from the composition memo rather than composed (compose_cache).
    cached: bool = field(default=False, compare=False)
    explicit_manifests: frozenset[Path] | None = None
    modules: ScopedModules | None = None

    @property
    def schema_layers(self) -> list[Layer]:
        """Only explicit roots and their dependencies define the authoring surface."""
        if self.explicit_manifests is None:
            return self.layers
        return [
            layer
            for layer in self.layers
            if layer.source == "project" or layer.manifest.resolve() in self.explicit_manifests
        ]

    @property
    def probes(self) -> list[ProbeDecl]:
        return [p for layer in self.layers for p in layer.probes]

    @property
    def providers(self) -> list[ProviderDecl]:
        return [p for layer in self.layers for p in layer.providers]


# --- package qualification -------------------------------------------------

# String literals and comments are opaque to qualification and name scans.
_OPAQUE = re.compile(r'"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/', re.DOTALL)


def _code_only(text: str) -> str:
    return _OPAQUE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def adt_branches(text: str) -> dict[str, set[str]]:
    """ADT type name -> its branch (constructor) names."""
    code = _code_only(text)
    out: dict[str, set[str]] = {}
    for adt in re.finditer(rf"^\s*\.type\s+({NAME})\s*=([^.]*)", code, re.MULTILINE):
        branches = set(re.findall(rf"(?:=|\|)\s*({NAME})\s*\{{", "=" + adt.group(2)))
        if branches:
            out.setdefault(adt.group(1), set()).update(branches)
    return out


def declared_names(text: str) -> set[str]:
    """Relation, type, ADT-branch and macro names a layer text declares."""
    code = _code_only(text)
    names = set(re.findall(rf"^\s*\.(?:decl|type)\s+({NAME})", code, re.MULTILINE))
    names |= set(re.findall(rf"^\s*#\s*define\s+({NAME})", code, re.MULTILINE))
    for branches in adt_branches(text).values():
        names |= branches
    return names


def qualification_prefix(package: str) -> str:
    return package.replace("-", "_")


def qualified_name(package: str, name: str) -> str:
    return f"{qualification_prefix(package)}__{name.replace('.', '__')}"


def qualify_names(text: str, package: str, names: set[str]) -> str:
    if not names:
        return text
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    # Not part of a longer name, and not an attribute/field name (`name:type`).
    pattern = re.compile(rf"(?<![\w.])({alternatives})(?!\w)(?!\.\w)(?!\s*:(?!-))")

    def code(segment: str) -> str:
        return pattern.sub(lambda m: qualified_name(package, m.group(1)), segment)

    out: list[str] = []
    pos = 0
    for m in _OPAQUE.finditer(text):
        out.append(code(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(code(text[pos:]))
    return "".join(out)


def qualify_layer_text(text: str, package: str, public: set[str]) -> str:
    return qualify_names(text, package, declared_names(text) - public)


def readable_names(
    open_rels: set[str], exports: set[str], branches: dict[str, set[str]]
) -> set[str]:
    """Names other packages may read: open relations, exports, exported ADT branches."""
    names = set(open_rels) | set(exports)
    for typ in exports:
        names |= branches.get(typ, set())
    return names


def public_names(
    open_rels: set[str],
    accepts: set[str],
    user: set[str],
    outputs: set[str],
    probes: list[ProbeDecl],
    exports: set[str] = frozenset(),
    branches: dict[str, set[str]] | None = None,
    driver: set[str] = frozenset(),
) -> set[str]:
    """Names kept unqualified: readable names, spec inputs, driver outputs, probe relations.

    Accepted, user, output and probe relations keep their names because the
    driver and fact files address them, but unless open or exported they are
    still private to their package: the checks reject reads from other
    packages.
    """
    names = (
        readable_names(open_rels, exports, branches or {}) | set(accepts) | set(user) | set(outputs)
    )
    names |= set(driver)
    for p in probes:
        names |= {p.request, p.domain, *p.results}
    return names


# --- Soufflé initial AST ---------------------------------------------------


def _statements(text: str) -> list[str]:
    out: list[str] = []
    start = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == ".":
            st = text[start : i + 1].strip()
            if st:
                out.append(st)
            start = i + 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _without_strings(text: str) -> str:
    out = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            out.append(" ")
        elif ch == '"':
            in_string = True
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def parse_initial_ast(text: str) -> ProgramInfo:
    info = ProgramInfo()
    clause_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(".decl "):
            m = re.match(rf"\.decl\s+({NAME})\s*\(([^)]*)\)", stripped)
            if m:
                attrs = [a.split(":", 1)[-1].strip() for a in m.group(2).split(",") if a.strip()]
                info.decls[m.group(1)] = tuple(attrs)
            continue
        if stripped.startswith(".type "):
            m = re.match(rf"\.type\s+({NAME})", stripped)
            if m:
                info.types.add(m.group(1))
            continue
        if stripped.startswith(FORBIDDEN):
            info.directives.append(stripped)
            names = re.findall(rf"({NAME})", stripped.split(None, 1)[1].split("(", 1)[0])
            if stripped.startswith(".input"):
                info.inputs.update(names)
            elif stripped.startswith((".output", ".printsize")):
                info.outputs.update(names)
            continue
        clause_lines.append(line)
    for st in _statements("\n".join(clause_lines)):
        m = re.match(rf"\s*({NAME})\s*\(", st)
        if not m:
            continue
        head = m.group(1)
        body: set[str] = set()
        if ":-" in st:
            rhs = _without_strings(st.split(":-", 1)[1])
            for atom in re.finditer(rf"(?<![$\w.])({NAME})\s*\(", rhs):
                rel = atom.group(1)
                if rel not in BUILTINS:
                    body.add(rel)
        info.clauses.append((head, body, st))
    return info


def ast_dump(files: list[Path], work: Path, name: str, *, preprocess: bool = False) -> str:
    work.mkdir(parents=True, exist_ok=True)
    combo = work / f"{name}.dl"
    combo.write_text("\n".join(f'#include "{f.as_posix()}"' for f in files) + "\n")
    root = Path(os.path.commonpath([work, *(f.parent for f in files)]))
    mode = "preprocessor" if preprocess else "--show=initial-ast"
    if preprocess:
        command = (
            ["mcpp", "-e", "utf8", "-W0"] if shutil.which("mcpp") else ["gcc", "-x", "c", "-E"]
        )
        version = souffle_cache.run(["--version"], [], root).stdout
        word_size = re.search(r"Word size:\s*(\d+)", version)
        if word_size is None:
            raise RuntimeError("cannot determine Souffle's RAM_DOMAIN_SIZE")
        cp = subprocess.run(
            [*command, "-I", str(combo.parent), f"-DRAM_DOMAIN_SIZE={word_size[1]}", str(combo)],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        cp = souffle_cache.run([mode, "-I", str(combo.parent), str(combo)], [combo, *files], root)
    (work / f"{name}.{'expanded' if preprocess else 'initial'}").write_text(cp.stdout)
    (work / f"{name}.stderr").write_text(cp.stderr)
    if cp.returncode:
        raise RuntimeError(f"souffle {mode} failed for {name}:\n{cp.stderr}")
    if not cp.stdout.strip():
        raise RuntimeError(f"could not parse Souffle initial AST for {name}: empty output")
    return cp.stdout


# --- manifests -------------------------------------------------------------


def _str_list(data: dict, key: str, where: str, errors: list[str]) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{where}: manifest key {key} must be a list of strings")
        return []
    return list(value)


def _parse_probe(raw: object, layer: str, manifest: Path, errors: list[str]) -> ProbeDecl | None:
    where = f"{layer}: probe"
    if not isinstance(raw, dict):
        errors.append(f"{where}: [[probe]] entries must be tables")
        return None
    where = f"{layer}: probe {raw.get('kind', '?')}"
    for key in sorted(set(raw) - PROBE_KEYS):
        errors.append(f"{where}: unknown probe key {key}")
    missing = [k for k in PROBE_REQUIRED if k not in raw]
    if missing:
        errors.append(f"{where}: missing probe key(s) {', '.join(missing)}")
        return None
    result = raw["result"]
    results = (result,) if isinstance(result, str) else result
    scalars = [raw[k] for k in ("kind", "request", "domain", "entry")]
    if not all(isinstance(v, str) for v in scalars) or not (
        isinstance(results, list | tuple) and results and all(isinstance(r, str) for r in results)
    ):
        errors.append(f"{where}: kind/request/domain/entry/result must be relation-name strings")
        return None
    for key in ("entry", "fingerprint"):
        if key in raw and (not isinstance(raw[key], str) or ":" not in raw[key]):
            errors.append(f"{where}: {key} must be `module:function` or `file.py:function`")
    params = raw.get("fingerprint_params", {})
    if not isinstance(params, dict):
        errors.append(f"{where}: fingerprint_params must be a table")
        params = {}
    unguarded = raw.get("unguarded")
    if unguarded is not None and (not isinstance(unguarded, str) or not unguarded.strip()):
        errors.append(f"{where}: unguarded must be a non-empty reason string")
        unguarded = None
    return ProbeDecl(
        str(raw["kind"]),
        str(raw["request"]),
        str(raw["domain"]),
        tuple(results),
        str(raw["entry"]),
        str(raw["fingerprint"]) if "fingerprint" in raw else None,
        tuple(sorted(params.items())),
        layer,
        manifest,
        unguarded,
    )


# `imports`: files of an installed package the `buildlog` provider hands to
# the provider of their format (a CMake export, a CPS or .pc file, the
# package's own facts file). The path is relative to the manifest, which sits
# at <prefix>/share/buildlog/<name>/, so at most three `..` keep it in the prefix.
IMPORT_KEYS = {"format", "file", "namespace"}
PREFIX_DEPTH = 3


def _parse_imports(data: dict, where: str, errors: list[str]) -> list[dict]:
    raw = data.get("imports", [])
    if not isinstance(raw, list):
        errors.append(f"{where}: imports must be an array of tables")
        return []
    out: list[dict] = []
    for r in raw:
        ok = (
            isinstance(r, dict)
            and not set(r) - IMPORT_KEYS
            and isinstance(r.get("format"), str)
            and PROVIDER_NAME.match(r["format"])
            and isinstance(r.get("file"), str)
            and isinstance(r.get("namespace", ""), str)
        )
        if not ok:
            errors.append(f"{where}: imports entries are {{ format, file[, namespace] }} tables")
            continue
        norm = posixpath.normpath(r["file"])
        parts = norm.split("/")
        up = len(list(itertools.takewhile(lambda x: x == "..", parts)))
        if posixpath.isabs(norm) or up > PREFIX_DEPTH:
            errors.append(f"{where}: imports file {r['file']} must stay inside the install prefix")
            continue
        out.append({"format": r["format"], "file": norm, "namespace": r.get("namespace", "")})
    return out


def _parse_stratum(
    raw: object, layer: str, manifest: Path, errors: list[str]
) -> StratumDecl | None:
    where = f"{layer}: stratum"
    if not isinstance(raw, dict):
        errors.append(f"{where}: [[stratum]] entries must be tables")
        return None
    where = f"{layer}: stratum {raw.get('name', '?')}"
    for key in sorted(set(raw) - STRATUM_KEYS):
        errors.append(f"{where}: unknown stratum key {key}")
    missing = [k for k in sorted(STRATUM_KEYS) if k not in raw]
    if missing:
        errors.append(f"{where}: missing stratum key(s) {', '.join(missing)}")
        return None
    name, program, entry = raw["name"], raw["program"], raw["entry"]
    if not all(isinstance(v, str) for v in (name, program, entry)) or ":" not in entry:
        errors.append(f"{where}: name, program (a file) and entry (`module:function`) are strings")
        return None
    return StratumDecl(name, (manifest.parent / program).resolve(), entry, layer, manifest)


def _parse_provider(
    raw: object, layer: str, manifest: Path, errors: list[str]
) -> ProviderDecl | None:
    where = f"{layer}: provider"
    if not isinstance(raw, dict):
        errors.append(f"{where}: [[provider]] entries must be tables")
        return None
    where = f"{layer}: provider {raw.get('name', '?')}"
    for key in sorted(set(raw) - PROVIDER_KEYS):
        errors.append(f"{where}: unknown provider key {key}")
    missing = [k for k in PROVIDER_REQUIRED if k not in raw]
    if missing:
        errors.append(f"{where}: missing provider key(s) {', '.join(missing)}")
        return None
    name, entry, rules = raw["name"], raw["entry"], raw.get("rules")
    if not isinstance(name, str) or not PROVIDER_NAME.match(name):
        errors.append(f"{where}: name must match {PROVIDER_NAME.pattern}")
        return None
    if not isinstance(entry, str) or ":" not in entry:
        errors.append(f"{where}: entry must be `module:object`")
        return None
    rules_dir = None
    if rules is not None:
        if not isinstance(rules, str):
            errors.append(f"{where}: rules must be a directory relative to the manifest")
        else:
            rules_dir = (manifest.parent / rules).resolve()
            if not (rules_dir / "manifest.toml").is_file():
                errors.append(f"{where}: rules {rules} is not a rule package (no manifest.toml)")
    return ProviderDecl(name, entry, rules_dir, layer, manifest)


def _requires(data: dict, where: str, errors: list[str]) -> dict[str, str]:
    value = data.get("requires", {})
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        errors.append(
            f'{where}: requires must be a table of version specifiers, e.g. {{ buildlog-cpp = ">=0.1" }}'
        )
        return {}
    for name, spec in value.items():
        try:
            SpecifierSet(spec)
        except InvalidSpecifier:
            errors.append(f"{where}: requires {name}: invalid version specifier {spec!r}")
    return dict(value)


def parse_manifest(path: Path, errors: list[str] | None = None) -> dict:
    """Parse and validate a manifest; problems are appended to `errors`."""
    errors = [] if errors is None else errors
    try:
        data = read_toml(path)
    except BuildlogError as exc:
        errors.append(str(exc))
        data = {}
    name = data.get("name")
    if not isinstance(name, str) or not PACKAGE_NAME.match(name):
        errors.append(f"{path}: manifest name must match {PACKAGE_NAME.pattern}")
        name = str(name)
    where = name
    for key in sorted(set(data) - MANIFEST_KEYS):
        errors.append(f"{where}: unknown manifest key {key}")
    rank = data.get("rank", 0)
    if not isinstance(rank, int) or rank < 0:
        errors.append(f"{where}: rank must be a non-negative integer")
        rank = 0
    version = data.get("version")
    try:
        Version(str(version))
    except InvalidVersion:
        errors.append(f"{where}: version must be a PEP 440 version, got {version!r}")
    namespace = data.get("namespace")
    if namespace is not None and (not isinstance(namespace, str) or not NAMESPACE.match(namespace)):
        errors.append(f"{where}: namespace must match {NAMESPACE.pattern}")
        namespace = None
    target_namespace = data.get("target_namespace", name)
    if not isinstance(target_namespace, str) or not TARGET_NAMESPACE.match(target_namespace):
        errors.append(f"{where}: target_namespace must match {TARGET_NAMESPACE.pattern}")
        target_namespace = name
    out: dict = {
        "name": name,
        "rank": rank,
        "version": None if version is None else str(version),
        "namespace": namespace,
        "target_namespace": target_namespace,
        "requires": _requires(data, where, errors),
    }
    for key in LIST_KEYS:
        out[key] = _str_list(data, key, where, errors)
    probes = data.get("probe", [])
    if not isinstance(probes, list):
        errors.append(f"{where}: probe must be an array of tables")
        probes = []
    out["probe"] = [p for p in (_parse_probe(r, name, path, errors) for r in probes) if p]
    providers = data.get("provider", [])
    if not isinstance(providers, list):
        errors.append(f"{where}: provider must be an array of tables")
        providers = []
    out["provider"] = [p for p in (_parse_provider(r, name, path, errors) for r in providers) if p]
    strata = data.get("stratum", [])
    if not isinstance(strata, list):
        errors.append(f"{where}: stratum must be an array of tables")
        strata = []
    out["stratum"] = [p for p in (_parse_stratum(r, name, path, errors) for r in strata) if p]
    out["imports"] = _parse_imports(data, where, errors)
    return out


def package_files(manifest: Path, data: dict) -> list[Path]:
    """The files a package's content hash covers: manifest, layers, facts and
    the files it imports."""
    listed = (*data["layers"], *data["facts"], *(i["file"] for i in data["imports"]))
    return [
        manifest,
        *(manifest.parent / f for f in listed),
        *(st.program for st in data["stratum"]),
    ]


def load_layer(
    ref: PackageRef,
    outdir: Path,
    work: Path,
    errors: list[str],
    layer_cache: dict | None = None,
) -> Layer:
    """Read a package, write its qualified files under `outdir`, and parse their AST.

    `layer_cache` (shared by the compositions of one configure) remembers a
    layer by what it was built from: its parsed manifest, the text of its
    files and where they go. A hit rewrites the qualified files and returns
    the same Layer; layers are not changed after loading."""
    manifest = ref.manifest
    data = parse_manifest(manifest, errors)
    name, rank = data["name"], data["rank"]
    files = [manifest.parent / f for f in data["layers"]]
    listed_facts = [manifest.parent / f for f in data["facts"]]
    imported = [manifest.parent / i["file"] for i in data["imports"]]
    for f in [*files, *listed_facts, *imported]:
        if not f.is_file():
            errors.append(f"{name}: manifest lists missing file {f}")
    files = [f for f in files if f.is_file()]
    fact_files = listed_facts
    texts = [f.read_text() for f in files]
    key = None
    if layer_cache is not None:
        digest = hashlib.sha256(repr((data, [str(f) for f in files], texts)).encode()).hexdigest()
        key = (str(outdir), ref.source, str(manifest), digest)
        hit = layer_cache.get(key)
        if hit is not None:
            layer, qualified_texts, layer_errors = hit
            for dst, dst_text in zip(layer.program_files, qualified_texts, strict=True):
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(dst_text)
            errors.extend(layer_errors)
            return layer
    errors_before = len(errors)
    text = "\n".join(texts)
    branches = adt_branches(text)
    exports = set(data["exports"])
    public = public_names(
        set(data["open"]),
        set(data["accepts"]),
        set(data["user"]),
        set(data["outputs"]),
        data["probe"],
        exports,
        branches,
        set(data["driver"]),
    )
    private = declared_names(text) - public
    program_files: list[Path] = []
    for src, src_text in zip(files, texts, strict=True):
        dst = outdir / name / src.relative_to(manifest.parent)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(qualify_names(src_text, name, private))
        program_files.append(dst)
    info = ProgramInfo()
    expanded = ""
    try:
        if program_files:
            expanded = ast_dump(program_files, work, name)
            info = parse_initial_ast(expanded)
    except RuntimeError as exc:
        errors.append(f"{name}: {exc}")
    qualified_branches = {
        qualified_name(name, typ) if typ in private else typ: {
            qualified_name(name, b) if b in private else b for b in bs
        }
        for typ, bs in branches.items()
    }
    layer = Layer(
        name=name,
        rank=rank,
        version=data["version"],
        source=ref.source,
        namespace=data["namespace"],
        target_namespace=data["target_namespace"],
        manifest=manifest,
        files=files,
        fact_files=fact_files,
        requires=data["requires"],
        extends=set(data["extends"]),
        open=set(data["open"]),
        exports=exports,
        accepts=set(data["accepts"]),
        user=set(data["user"]),
        driver=set(data["driver"]),
        outputs=set(data["outputs"]),
        registered_includes=set(data["registered_includes"]),
        probes=data["probe"],
        providers=data["provider"],
        qualified={n: qualified_name(name, n) for n in sorted(private)},
        readable=readable_names(set(data["open"]), exports, branches),
        program_files=program_files,
        info=info,
        text=text,
        branches=qualified_branches,
        imports=data["imports"],
        strata=data["stratum"],
        expanded=expanded,
    )
    if key is not None:
        layer_cache[key] = (layer, [f.read_text() for f in program_files], errors[errors_before:])
    return layer


def load_project_layer(
    files: list[Path], lower: list[Layer], outdir: Path, work: Path, errors: list[str]
) -> Layer | None:
    if not files:
        return None
    name = "project"
    rank = max((layer.rank for layer in lower), default=0) + 1
    program_files: list[Path] = []
    for src in files:
        dst = outdir / name / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text())
        program_files.append(dst)
    info = ProgramInfo()
    text = "\n".join(src.read_text() for src in files)
    try:
        info = parse_initial_ast(ast_dump(program_files, work, name))
    except RuntimeError as exc:
        errors.append(f"{name}: {exc}")
    open_rels = {rel for layer in lower for rel in layer.open}
    readable = {rel for layer in lower for rel in (layer.open | layer.exports)}
    return Layer(
        name=name,
        rank=rank,
        version=None,
        source="project",
        namespace=None,
        target_namespace="",
        manifest=files[0],
        files=files,
        fact_files=[],
        requires={layer.name: ">=0" for layer in lower},
        extends=open_rels,
        open=set(),
        exports=set(),
        accepts=set(info.decls),
        user=set(),
        driver=set(),
        outputs=set(),
        registered_includes=set(),
        probes=[],
        providers=[],
        qualified={},
        readable=readable,
        program_files=program_files,
        info=info,
        text=text,
        branches=adt_branches(text),
    )


# --- checks ----------------------------------------------------------------


def _dependencies(layers: list[Layer]) -> dict[str, set[str]]:
    """Package -> the packages it may read or extend: transitive requires plus the root."""
    by_name = {layer.name: layer for layer in layers}
    roots = {layer.name for layer in layers if layer.source == "core"}
    deps: dict[str, set[str]] = {}
    for layer in layers:
        seen: set[str] = set()
        todo = list(layer.requires)
        while todo:
            n = todo.pop()
            if n in seen or n not in by_name:
                continue
            seen.add(n)
            todo.extend(by_name[n].requires)
        deps[layer.name] = (seen | roots) - {layer.name}
    return deps


def _check_manifests(layers: list[Layer]) -> list[str]:
    errors: list[str] = []
    by_name = {layer.name: layer for layer in layers}
    prefixes: dict[str, str] = {}
    namespaces: dict[str, str] = {}
    providers_seen: dict[str, str] = {}
    qualifiers: dict[str, str] = {}
    for layer in layers:
        folded = layer.target_namespace.casefold()
        if folded in qualifiers:
            errors.append(
                f"{layer.name}: target namespace {layer.target_namespace} collides with "
                f"{qualifiers[folded]}"
            )
        qualifiers.setdefault(folded, layer.name)
        prefix = qualification_prefix(layer.name)
        if prefix in prefixes:
            errors.append(
                f"{layer.name}: qualification prefix {prefix}__ collides with {prefixes[prefix]}"
            )
        prefixes.setdefault(prefix, layer.name)
        if layer.namespace:
            if layer.namespace in namespaces:
                errors.append(
                    f"{layer.name}: namespace {layer.namespace} is already owned by "
                    f"{namespaces[layer.namespace]}"
                )
            namespaces.setdefault(layer.namespace, layer.name)
        if layer.tier == 2 and layer.probes:
            errors.append(
                f"{layer.name}: rule package found on a search path declares [[probe]] "
                f"({', '.join(p.kind for p in layer.probes)}); only registered Python plugins "
                "(entry point group buildlog.plugins) may run probes"
            )
        if layer.tier == 2 and layer.providers:
            errors.append(
                f"{layer.name}: rule package found on a search path declares [[provider]] "
                f"({', '.join(p.name for p in layer.providers)}); only registered Python "
                "plugins (entry point group buildlog.plugins) may provide package discovery"
            )
        for pv in layer.providers:
            if pv.name in providers_seen:
                errors.append(
                    f"{layer.name}: provider {pv.name} is already registered by "
                    f"{providers_seen[pv.name]}"
                )
            providers_seen.setdefault(pv.name, layer.name)
        for req, spec in sorted(layer.requires.items()):
            dep = by_name.get(req)
            if dep is None:
                errors.append(f"{layer.name}: requires {req}, which is not loaded")
                continue
            if dep.rank >= layer.rank:
                errors.append(f"{layer.name}: requires {req}, which is not a lower layer")
            try:
                ok = Version(str(dep.version)) in SpecifierSet(spec)
            except (InvalidSpecifier, InvalidVersion):
                continue  # reported by parse_manifest
            if not ok:
                errors.append(
                    f"{layer.name}: requires {req} {spec}, but {req} {dep.version} is loaded"
                )
    for layer in layers:
        if not layer.namespace:
            continue
        mine = f"{layer.namespace}_"
        extended = layer.extends
        for key, rels in (
            ("declares", layer.raw_decls),
            ("accepts", layer.accepts),
            ("user", layer.user),
            ("driver", layer.driver),
            ("opens", layer.open),
            ("exports", {r for r in layer.exports if r in layer.raw_decls}),
            ("outputs", layer.outputs),
        ):
            for rel in sorted(rels - extended):
                if not rel.startswith(mine):
                    errors.append(
                        f"{layer.name}: {key} {rel}, outside its namespace (must start with {mine})"
                    )
        for other in layers:
            if other is layer:
                continue
            public = (
                other.open
                | other.exports
                | other.accepts
                | other.user
                | other.driver
                | other.outputs
            )
            public |= {rel for p in other.probes for rel in (p.request, p.domain, *p.results)}
            for rel in sorted(public):
                if rel.startswith(mine):
                    errors.append(
                        f"{other.name}: public relation {rel} is in namespace {layer.namespace}, "
                        f"owned by {layer.name}"
                    )
    return errors


def _check(
    layers: list[Layer],
    required_outputs: tuple[str, ...],
    required_inputs: tuple[str, ...] = (),
) -> list[str]:
    errors: list[str] = []
    owner: dict[str, str] = {}
    owner_rank: dict[str, int] = {}
    type_owner: dict[str, str] = {}
    branch_owner: dict[str, str] = {}
    by_name = {layer.name: layer for layer in layers}
    deps = _dependencies(layers)
    # Raw private names of each package, to explain references that
    # qualification turned into undefined names.
    private_of: dict[str, set[str]] = {}
    for layer in layers:
        for raw in layer.qualified:
            private_of.setdefault(raw, set()).add(layer.name)
    for layer in layers:
        for d in layer.info.directives:
            for raw, qualified in layer.qualified.items():
                d = re.sub(rf"\b{re.escape(qualified)}\b", raw, d)
            errors.append(
                f"{layer.name}: forbidden directive {d.split()[0]} in layer ({d}); "
                "layers carry no IO, the loader generates it from the manifest"
            )
        for inc in re.findall(r'^\s*#include\s+"([^"]+)"', layer.text, re.MULTILINE):
            if inc not in layer.registered_includes:
                errors.append(f"{layer.name}: unregistered include {inc}")
        for typ in sorted(layer.info.types):
            if typ in type_owner and type_owner[typ] != layer.name:
                errors.append(f"{layer.name}: redeclares type {typ} owned by {type_owner[typ]}")
            type_owner.setdefault(typ, layer.name)
        for branches in layer.branches.values():
            for b in branches:
                branch_owner.setdefault(b, layer.name)
        for rel in sorted(layer.info.decls):
            if rel in owner and owner[rel] != layer.name:
                errors.append(f"{layer.name}: redeclares relation {rel} owned by {owner[rel]}")
            owner.setdefault(rel, layer.name)
            owner_rank.setdefault(rel, layer.rank)
    errors += _check_manifests(layers)
    errors += _check_import_ownership(layers)

    def private_hint(name: str, reader: Layer) -> str | None:
        others = sorted(private_of.get(name, set()) - {reader.name})
        return f"private to {', '.join(others)}" if others else None

    def check_read(
        layer: Layer, what: str, name: str, table: dict[str, str], context: str = ""
    ) -> None:
        """A reference from `layer` to a name declared by another package."""
        pkg = table.get(name)
        if pkg is None:
            hint = private_hint(name, layer)
            if hint:
                errors.append(
                    f"{layer.name}: {what} {name}{context} is {hint} (not exported or open)"
                )
            return
        if pkg == layer.name:
            return
        if name not in by_name[pkg].readable:
            errors.append(
                f"{layer.name}: {what} {name}{context} is private to {pkg} (not exported or open)"
            )
        elif pkg not in deps[layer.name]:
            errors.append(f"{layer.name}: reads {name} from {pkg}, which it does not require")

    for layer in layers:
        # A private name equal to another layer's name would be qualified away
        # silently; it is a redeclaration of that layer's relation or type.
        for raw in layer.qualified:
            for kind, table in (("relation", owner), ("type", type_owner)):
                if raw in table and table[raw] != layer.name:
                    errors.append(f"{layer.name}: redeclares {kind} {raw} owned by {table[raw]}")
        own_types = set(layer.info.types)
        for key, rels in (
            ("open", layer.open),
            ("accepts", layer.accepts),
            ("user", layer.user),
            ("driver", layer.driver),
        ):
            for rel in sorted(rels):
                if owner.get(rel) != layer.name:
                    errors.append(f"{layer.name}: {key} lists {rel}, which it does not declare")
        for rel in sorted(layer.outputs):
            if owner.get(rel) != layer.name:
                errors.append(f"{layer.name}: outputs lists {rel}, which it does not declare")
        for name in sorted(layer.exports):
            if owner.get(name) != layer.name and name not in own_types:
                errors.append(
                    f"{layer.name}: exports lists {name}, which it does not declare "
                    "(exports name own relations or types)"
                )
        raw = {q: r for r, q in layer.qualified.items()}
        for rel, attrs in sorted(layer.info.decls.items()):
            for typ in attrs:
                if typ not in PRIMITIVE_TYPES:
                    check_read(layer, "type", typ, type_owner, f" (in {raw.get(rel, rel)})")

    open_by: dict[str, Layer] = {}
    for layer in layers:
        for rel in layer.open:
            if owner.get(rel) == layer.name:
                open_by[rel] = layer
    for layer in layers:
        for rel in sorted(layer.extends):
            opener = open_by.get(rel)
            if opener is not None and opener.rank < layer.rank:
                if opener.name not in deps[layer.name]:
                    errors.append(
                        f"{layer.name}: extends {rel} of {opener.name}, which it does not require"
                    )
                continue
            if rel not in owner:
                why = "no layer declares it"
            elif opener is None:
                why = f"owned by {owner[rel]}, not in its open registry"
            else:
                why = f"opened by {opener.name}, which is not a lower layer"
            errors.append(
                f"{layer.name}: extends {rel}, which is not an open relation of a lower layer "
                f"({why})"
            )

    heads: set[str] = set()
    for layer in layers:
        for head, body, text in layer.info.clauses:
            heads.add(head)
            howner = owner.get(head)
            if howner is None:
                hint = private_hint(head, layer)
                why = f"is {hint}" if hint else "is declared by no loaded package"
                errors.append(f"{layer.name}: clause head {head} {why}")
            elif howner != layer.name and head not in layer.extends:
                errors.append(
                    f"{layer.name}: clause head {head} is owned by {howner}; not declared in extends"
                )
            for rel in sorted(body):
                check_read(layer, "body relation", rel, owner)
                if rel not in owner:
                    continue
                if owner[rel] != layer.name and owner_rank[rel] > layer.rank:
                    errors.append(
                        f"{layer.name}: body relation {rel} belongs to higher layer {owner[rel]}"
                    )
            for branch in sorted(set(re.findall(rf"\$({NAME})\s*\(", _without_strings(text)))):
                check_read(layer, "constructor", branch, branch_owner)

    errors += _check_probes(layers, owner, heads)
    errors += _check_answer_reads(layers)
    for rel in required_outputs:
        if rel not in owner:
            errors.append(f"relation {rel} is read by the driver but declared by no layer")
    # `driver` relations are an exact contract with the engine, which writes
    # their rows (spec and package facts may not).
    listed = {rel: layer.name for layer in layers for rel in layer.driver}
    for rel in required_inputs:
        if rel not in listed:
            errors.append(
                f"relation {rel} is written by the driver but no manifest lists it in driver"
            )
    for rel, name in sorted(listed.items()):
        if rel not in required_inputs:
            errors.append(f"{name}: driver lists {rel}, which the driver does not write")
    return errors


# (W5): the relations deciding which targets are requested and their kinds
# (formalization.tex, "decision relations"): request, request_all and
# native_kind, the relation giving a target's kind (target_kind and
# target_kind_when feed it, so reachability covers them).
DECISION_RELATIONS = ("request", "request_all", "native_kind")


def _check_discovery_checks(layers: list[Layer]) -> list[str]:
    """(W4): discovery-time checks cannot reach completion-gated rules.

    Per-round checks read requests and domains; activation consistency also
    reads discovered_rule_package. Answer-shape checks read Python return
    values, not derived relations. Conservatively protect every head of a rule
    reading discovery_complete, not just the core's propagated-error heads.
    """
    declared = {rel for layer in layers for rel in layer.info.decls}
    edges: dict[str, set[str]] = {}
    completion = {
        layer.qualified.get("discovery_complete", "discovery_complete") for layer in layers
    }
    gated = set(completion)
    for layer in layers:
        for head, body, _ in layer.info.clauses:
            bodies = body & declared
            edges.setdefault(head, set()).update(bodies)
            if completion & bodies:
                gated.add(head)
    roots = {"discovered_rule_package"}
    roots.update(r for layer in layers for p in layer.probes for r in (p.request, p.domain))
    errors = []
    for root in sorted(roots & declared):
        paths = {root: [root]}
        queue = [root]
        for rel in queue:
            if rel in gated:
                errors.append(
                    f"{root} depends on completion-gated relation {rel} "
                    f"(W4: probe requests, domains and discovery-time check relations "
                    f"must not depend on propagated errors or discovery_complete): "
                    f"{' <- '.join(paths[rel])}"
                )
                break
            for body in sorted(edges.get(rel, ())):
                if body not in paths:
                    paths[body] = [*paths[rel], body]
                    queue.append(body)
    return errors


def _check_decision_relations(layers: list[Layer]) -> list[str]:
    """(W5): no decision relation reaches a probe-result relation.

    The predicate dependency graph has an edge from each rule's head to every
    relation in its body, positive, negated or inside an aggregate, plus the
    generated probe_pending rules (a request and its primary result). A path
    from a decision relation to a result means the requested targets or their
    kinds could depend on a probe answer, which differs between machines."""
    results = {r for layer in layers for p in layer.probes for r in p.results}
    if not results:
        return []
    # Only declared relations are nodes: ADT constructors and functors parse as
    # atoms too, and `opt_holds(EQ(k, v))` must not make EQ a head.
    declared = {rel for layer in layers for rel in layer.info.decls}
    edges: dict[str, dict[str, str]] = {}
    for layer in layers:
        for where, heads, disjuncts in _clauses(layer):
            for _, head, _ in heads:
                for d in disjuncts:
                    for _, body, _ in d:
                        if head in declared and body in declared:
                            edges.setdefault(head, {}).setdefault(body, where)
        for p in layer.probes:
            for body in (p.request, p.results[0]):
                edges.setdefault("probe_pending", {}).setdefault(body, "generated rule:0")
    errors = []
    for root in DECISION_RELATIONS:
        prev: dict[str, str | None] = {root: None}
        queue = [root]
        hit = None
        for rel in queue:
            if rel in results:
                hit = rel
                break
            for body in edges.get(rel, ()):
                if body not in prev:
                    prev[body] = rel
                    queue.append(body)
        if hit is None:
            continue
        path = [hit]
        while prev[path[-1]] is not None:
            path.append(prev[path[-1]])
        path.reverse()
        steps = [root]
        for head, body in itertools.pairwise(path):
            file, line = edges[head][body].rsplit(":", 1)
            if file == "generated rule":
                steps.append(f"{body} (generated rule)")
                continue
            rel = os.path.relpath(file)
            steps.append(f"{body} ({file if rel.startswith('..') else rel}:{line})")
        errors.append(
            f"{root} depends on the probe result {hit}, so what is requested would vary with "
            f"probe answers (W5: request, request_all and native_kind must not depend on probe "
            f"results, positively or negatively): {' <- '.join(steps)}"
        )
    return errors


def _check_probes(layers: list[Layer], owner: dict[str, str], heads: set[str]) -> list[str]:
    errors: list[str] = []
    kinds: dict[str, str] = {}
    claimed: dict[str, str] = {}
    for layer in layers:
        decls = layer.info.decls
        for p in layer.probes:
            where = f"{layer.name}: probe {p.kind}"
            if p.kind in kinds:
                errors.append(f"{where}: duplicate probe kind (also declared by {kinds[p.kind]})")
            kinds.setdefault(p.kind, layer.name)
            for role, rel in (
                ("request", p.request),
                ("domain", p.domain),
                *(("result", r) for r in p.results),
            ):
                if rel not in decls:
                    found = f" (it is owned by {owner[rel]})" if rel in owner else ""
                    errors.append(
                        f"{where}: {role} relation {rel} is not declared by {layer.name}{found}"
                    )
                if rel in claimed:
                    errors.append(f"{where}: {role} relation {rel} already used by {claimed[rel]}")
                claimed.setdefault(rel, f"probe {p.kind}")
                if rel in layer.accepts:
                    errors.append(
                        f"{where}: {role} relation {rel} must not be listed in accepts; the "
                        "loader generates probe IO"
                    )
            req, dom = decls.get(p.request), decls.get(p.domain)
            if req is not None and dom is not None and req != dom:
                errors.append(
                    f"{where}: request {p.request}({', '.join(req)}) and domain "
                    f"{p.domain}({', '.join(dom)}) must have the same attribute types"
                )
            for rel in p.results:
                if rel in heads:
                    errors.append(
                        f"{where}: result relation {rel} has rule clauses; results are "
                        "loader-generated .input relations filled only by the probe"
                    )
                res = decls.get(rel)
                if req and res is not None and (not res or res[0] != req[0]):
                    errors.append(
                        f"{where}: result relation {rel} must start with the request id "
                        f"(type {req[0]})"
                    )
    return errors


def _probe_pending_rules(layers: list[Layer]) -> list[str]:
    """Rules for core's probe_pending(id): a request of any probe that has no
    row in the probe's primary result relation, which is what the driver's
    has_answer requires of every answer. The names and arities come from the
    probe declarations, so core need not know the plugins' probes."""
    if not any("probe_pending" in layer.info.decls for layer in layers):
        return []
    rules = ["// probe_pending: a request nobody has answered yet."]
    for layer in layers:
        for p in layer.probes:
            req = layer.info.decls[p.request]
            res = layer.info.decls[p.results[0]]
            rules.append(
                f"probe_pending(id) :- {p.request}({', '.join(['id', *'_' * (len(req) - 1)])}), "
                f"!{p.results[0]}({', '.join(['id', *'_' * (len(res) - 1)])})."
            )
    return rules


_VAR = re.compile(r"[A-Za-z]\w*\Z")


def _split_top(text: str, sep: str) -> list[str]:
    """Split masked text at `sep` outside brackets."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _atoms(text: str) -> list[tuple[bool, str, str]]:
    """(non-positive, relation, first argument) of each atom in masked text.

    A nested atom is not certified as a positive premise of the enclosing rule:
    it may be aggregate-local or belong to only one branch of a disjunction.
    """
    out = []
    for m in re.finditer(r"(!?)\s*(?<![$\w.])([A-Za-z_][\w.]*)\s*\(", text):
        depth, j = 1, m.end()
        while depth and j < len(text):
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        first = _split_top(text[m.end() : j - 1], ",")[0].strip()
        prefix = text[: m.start()]
        nested = prefix.count("{") > prefix.count("}") or prefix.count("(") > prefix.count(")")
        out.append((m.group(1) == "!" or nested, m.group(2), first))
    return out


def _clauses(layer: Layer, *, include_facts: bool = False):
    """(source file:line, head atoms, body disjuncts) of each rule of a layer."""
    from .provenance.program import _mask, _statements

    for src, dst in zip(layer.files, layer.program_files, strict=False):
        code = _mask(dst.read_text())
        for start, end, kind in _statements(code):
            clause = code[start:end]
            if kind != "clause":
                continue
            if ":-" not in clause:
                if not include_facts:
                    continue
                head, body = clause, ""
            else:
                head, body = clause.split(":-", 1)
            line = code.count("\n", 0, start) + 1
            yield f"{src}:{line}", _atoms(head), [_atoms(d) for d in _split_top(body, ";")]


# A package that imports files states facts about the targets and the package
# name of what it imports, and nothing else: the relations keyed by a target
# id (first argument), and the ones keyed by a package name.
OWNED_TARGET_RELATIONS = (
    "target",
    "imported",
    "imported_kind",
    "import_artifact",
    "prop",
    "prop_when",
    "dep",
    "dep_when",
    "package_want",
)
OWNED_PACKAGE_RELATIONS = ("package_version", "package_attr", "import_linkage_dep")
_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"\Z')


def _args(masked: str, original: str, start: int) -> tuple[list[tuple[str, str]], int]:
    """(masked, original) of each top-level argument of the call opened at
    `start` (just after its parenthesis), and the offset after the call."""
    depth, j = 1, start
    while depth and j < len(masked):
        depth += {"(": 1, ")": -1}.get(masked[j], 0)
        j += 1
    out, depth, a = [], 0, start
    for i in range(start, j - 1):
        ch = masked[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append((masked[a:i], original[a:i]))
            a = i + 1
    out.append((masked[a : j - 1], original[a : j - 1]))
    return out, j


def _calls(masked: str, original: str):
    for m in re.finditer(r"(!?)\s*(?<![$\w.])([A-Za-z_][\w.]*)\s*\(", masked):
        args, _ = _args(masked, original, m.end())
        yield m.group(1) == "!", m.group(2), [o.strip() for _, o in args]


def _literal_prefix(arg: str) -> str | None:
    """The leading string of a target id: "X::y", or cat("X::", ...)."""
    m = _STRING.match(arg)
    if m:
        return m.group(1)
    m = re.match(r'cat\(\s*"((?:[^"\\\n]|\\.)*)"', arg)
    return m.group(1) if m else None


def _check_import_ownership(layers: list[Layer]) -> list[str]:
    """A package that imports files asserts only about its own namespaces.

    The namespaces a package owns are its name, its target_namespace and the
    namespace of each of its imports. A clause head of a target-keyed relation
    must name a target of one of them: a literal or a cat() starting with one
    (`X::y`, `X:y`), or a variable bound in the body by
    import_ns_target(<owned>, v); a package-keyed head (package_version,
    package_attr, ...) names the package. Otherwise it would assert facts
    about another package's targets, which that package's own rules and files
    are the only source of."""
    from .provenance.program import _mask, _statements

    errors = []
    for layer in layers:
        if layer.tier != 2 or not layer.imports:
            continue
        owned = {
            layer.name,
            layer.target_namespace,
            *(i["namespace"] for i in layer.imports if i.get("namespace")),
        }
        for src, dst in zip(layer.files, layer.program_files, strict=False):
            original = dst.read_text()
            code = _mask(original)
            for start, end, kind in _statements(code):
                if kind != "clause":
                    continue
                at = code.find(":-", start, end)
                at = end if at < 0 else at
                heads = list(_calls(code[start:at], original[start:at]))
                body = list(_calls(code[at:end], original[at:end]))
                line = code.count("\n", 0, start) + 1
                bound = {
                    a[1]
                    for neg, rel, a in body
                    if not neg
                    and rel == "import_ns_target"
                    and len(a) == 2
                    and _literal_prefix(a[0]) in owned
                }
                for _, rel, args in heads:
                    if rel not in OWNED_TARGET_RELATIONS + OWNED_PACKAGE_RELATIONS or not args:
                        continue
                    first = args[0]
                    prefix = _literal_prefix(first)
                    if rel in OWNED_PACKAGE_RELATIONS:
                        ok = prefix in owned and _STRING.match(first) is not None
                    else:
                        ok = first in bound or (
                            prefix is not None
                            and re.split(r":", prefix, maxsplit=1)[0] in owned
                            and ":" in prefix
                        )
                    if not ok:
                        errors.append(
                            f"{layer.name}: {src}:{line}: {rel}({first}, ...) asserts about a target "
                            f"outside the namespaces its imports own ({', '.join(sorted(owned))})"
                        )
    return errors


def _check_answer_reads(layers: list[Layer]) -> list[str]:
    """Probe results are read only joined with their request.

    A rule that reads a result `res(id, ...)` must bind `id`, a variable, in
    a positive atom of the probe's request relation, or of a relation all of
    whose rules bind their first argument that way (a guard, e.g. a request
    joined with its answer). Then an answer whose request does not arise
    derives nothing, so the driver may seed cached answers before their
    requests (discovery.run_fixpoint) without changing any result. A probe
    whose results a rule must read otherwise (below its request, for
    stratification) states why with `unguarded = "reason"`; its answers are
    then seeded only when the whole previous derivation is replayed."""
    probes = {p.request: p for layer in layers for p in layer.probes}
    result_of = {r: p for p in probes.values() for r in p.results}
    clauses = [(layer, *c) for layer in layers for c in _clauses(layer, include_facts=True)]
    # Also check expanded clauses, so macros and includes cannot bypass guards.
    # Keep source clauses for precise diagnostics and conservative certification.
    from .provenance.program import _mask

    for layer in layers:
        known = {
            repr((heads, disjuncts)): where
            for la, where, heads, disjuncts in clauses
            if la is layer
        }
        for _, _, text in layer.info.clauses:
            parts = _mask(text).split(":-", 1)
            heads = _atoms(parts[0])
            disjuncts = [_atoms(d) for d in _split_top(parts[1] if len(parts) == 2 else "", ";")]
            signature = repr((heads, disjuncts))
            if signature not in known:
                where = f"{layer.files[0]}:1 (preprocessed clause)"
                clauses.append((layer, where, heads, disjuncts))
                known[signature] = where
    inputs = {r for layer in layers for r in layer.accepts | layer.user | layer.driver}
    guards = {req: {req} for req in probes}
    changed = True
    while changed:
        changed = False
        rules: dict[str, list[bool]] = {}
        for _, _, heads, disjuncts in clauses:
            for rel, first in ((r, f) for _, r, f in heads):
                for req, gs in guards.items():
                    ok = bool(_VAR.match(first)) and all(
                        any(not neg and r in gs and f == first for neg, r, f in d)
                        for d in disjuncts
                    )
                    rules.setdefault(f"{req}\0{rel}", []).append(ok)
        for key, oks in rules.items():
            req, rel = key.split("\0")
            if all(oks) and rel not in guards[req] and rel not in result_of and rel not in inputs:
                guards[req].add(rel)
                changed = True
    errors = []
    for layer, where, _, disjuncts in clauses:
        for d in disjuncts:
            for neg, rel, first in d:
                p = result_of.get(rel)
                if p is None or p.unguarded:
                    continue
                if _VAR.match(first) and any(
                    not n and r in guards[p.request] and f == first for n, r, f in d
                ):
                    continue
                errors.append(
                    f"{where}: {'!' if neg else ''}{rel} is read without its request: bind its "
                    f"first argument (the request id) in {p.request}(id, ...) in the same body, "
                    f'or state why the probe cannot (unguarded = "reason" in {p.layer}\'s '
                    f"[[probe]] {p.kind})"
                )
    return errors


# --- composition -----------------------------------------------------------


from buildlog.gated import check_layers as check_gated_recursion


def _split_top(text: str, separator: str) -> list[str]:
    parts = []
    start = depth = 0
    for offset, char in enumerate(_without_strings(text)):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:offset])
            start = offset + 1
    return [*parts, text[start:]]


def _source_declarations(program: Program, outdir: Path) -> Path | None:
    """Reserve source names without depending on the metadata relay."""
    core = next((layer for layer in program.layers if layer.source == "core"), None)
    if core is None or "source_target" not in core.qualified:
        return None
    names = {
        "target": core.qualified["source_target"],
        "imported": core.qualified["source_imported"],
        "alias": core.qualified["source_alias"],
    }
    clauses = set()
    for layer in program.schema_layers:
        if layer.source == "core":
            continue
        for _, _, text in layer.info.clauses:
            at = _without_strings(text).find(":-")
            heads = text[:at] if at >= 0 else text.rstrip().removesuffix(".")
            body = text[at:] if at >= 0 else "."
            for atom in _split_top(heads, ","):
                match = re.match(r"\s*(target|imported|alias)\s*(\(.*)", atom, re.DOTALL)
                if match is None or (match[1] == "imported" and at >= 0):
                    continue
                clauses.add(names[match[1]] + match[2] + " " + body)
    path = outdir / "source-declarations.dl"
    path.write_text("\n".join(sorted(clauses)) + "\n")
    return path


def compose_program(
    packages: list[PackageRef | Path],
    outdir: Path,
    work: Path,
    required_outputs: tuple[str, ...] = (),
    required_inputs: tuple[str, ...] = (),
    project_files: list[Path] | None = None,
    layer_cache: dict | None = None,
    *,
    composed_check: Callable[[Path, list[Layer], list[Path]], list[str]] | None = None,
    cache_dir: Path | None = None,
    explicit_manifests: frozenset[Path] | None = None,
) -> Program:
    """Qualify, check and compose layers into `outdir/build.dl` with generated IO.

    `required_outputs` are relations the driver reads (emitter, `error`). On any
    check error `Program.build` is None and nothing runnable is written. A bare
    Path is a tier-2 (search path) package. A composed_check may reuse the
    evaluator's startup checks instead of launching a separate check process.
    With `cache_dir`, a successful composition is memoized there under a hash of
    everything it reads (compose_cache); a hit restores the program and its files.
    `explicit_manifests` limits the authoring schema, not executable membership.
    It must include the explicit roots' dependency closure.
    """
    explicit_manifests = (
        None if explicit_manifests is None else frozenset(p.resolve() for p in explicit_manifests)
    )
    memo = None
    if cache_dir is not None and compose_cache.enabled():
        args = (
            packages,
            outdir,
            work,
            required_outputs,
            required_inputs,
            project_files,
            explicit_manifests,
        )
        memo = compose_cache.key(*args)
        if memo is not None and (hit := compose_cache.load(cache_dir, memo, outdir, work)):
            return hit
    program = _compose_uncached(
        packages,
        outdir,
        work,
        required_outputs,
        required_inputs,
        project_files,
        layer_cache,
        composed_check=composed_check,
        explicit_manifests=explicit_manifests,
    )
    # Stored only if nothing it read changed while it ran.
    if memo is not None and program.build is not None and compose_cache.key(*args) == memo:
        compose_cache.store(cache_dir, memo, program, outdir)
    return program


def _compose_uncached(
    packages: list[PackageRef | Path],
    outdir: Path,
    work: Path,
    required_outputs: tuple[str, ...],
    required_inputs: tuple[str, ...],
    project_files: list[Path] | None,
    layer_cache: dict | None,
    *,
    composed_check: Callable[[Path, list[Layer], list[Path]], list[str]] | None,
    explicit_manifests: frozenset[Path] | None = None,
) -> Program:
    for d in (outdir, work):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    errors: list[str] = []
    layers: list[Layer] = []
    seen: dict[str, Path] = {}
    for pkg in packages:
        ref = pkg if isinstance(pkg, PackageRef) else PackageRef(pkg)
        if ref.source not in SOURCES:
            raise ValueError(f"unknown package source {ref.source!r}")
        try:
            name = read_toml(ref.manifest).get("name")
        except BuildlogError as exc:
            errors.append(str(exc))
            name = ref.manifest.parent.name
        if name in seen:
            errors.append(f"{name}: layer loaded twice ({seen[name]} and {ref.manifest})")
            continue
        seen[name] = ref.manifest
        layers.append(load_layer(ref, outdir, work, errors, layer_cache))
    schema_layers = layers
    if explicit_manifests is not None:
        schema_layers = [
            layer for layer in layers if layer.manifest.resolve() in explicit_manifests
        ]
        missing = explicit_manifests - {layer.manifest.resolve() for layer in schema_layers}
        errors += [
            f"{p}: explicit schema root is not in the executable union" for p in sorted(missing)
        ]
        by_name = {layer.name: layer for layer in layers}
        for layer in schema_layers:
            for name in layer.requires:
                dependency = by_name.get(name)
                if (
                    dependency is not None
                    and dependency.manifest.resolve() not in explicit_manifests
                ):
                    errors.append(
                        f"{layer.manifest}: explicit schema scope omits required package {name}"
                    )
    if project_files:
        project_layer = load_project_layer(project_files, schema_layers, outdir, work, errors)
        if project_layer is not None:
            project_layer.rank = max((layer.rank for layer in layers), default=0) + 1
            layers.append(project_layer)
    layers.sort(key=lambda layer: (layer.rank, layer.name))
    errors += _check(layers, required_outputs, required_inputs)
    errors += _check_strata(layers)
    errors = list(dict.fromkeys(errors))
    program = Program(None, layers, errors, explicit_manifests=explicit_manifests)
    if errors:
        return program

    from .modules import compile_modules

    program.modules = compile_modules(program, outdir)
    if program.errors:
        return program
    inputs: set[str] = set()
    outputs: set[str] = set(required_outputs)
    for layer in layers:
        inputs |= layer.accepts | layer.user | layer.driver
        outputs |= layer.outputs
        for p in layer.probes:
            inputs.update(p.results)
            outputs.update((p.request, p.domain))
    if program.modules is not None:
        inputs |= program.modules.inputs
    program.inputs, program.outputs = sorted(inputs), sorted(outputs)
    program.driver_inputs = sorted(rel for layer in layers for rel in layer.driver)
    if program.modules is not None:
        program.driver_inputs = sorted(set(program.driver_inputs) | program.modules.inputs)
    io = ["// Generated by the buildlog loader from layer manifests."]
    io += [f".input {rel}" for rel in program.inputs]
    io += [f".output {rel}" for rel in program.outputs]
    io += _probe_pending_rules(layers)
    (outdir / "io.dl").write_text("\n".join(io) + "\n")
    executable_files = [
        file
        for layer in layers
        for file in (
            [program.modules.programs[layer.manifest.resolve()]]
            if program.modules is not None and layer.manifest.resolve() in program.modules.programs
            else layer.program_files
        )
    ]
    includes = [file.relative_to(outdir).as_posix() for file in executable_files]
    declarations = _source_declarations(program, outdir)
    if declarations is not None:
        includes.append(declarations.relative_to(outdir).as_posix())
    build = outdir / "build.dl"
    build.write_text("".join(f'#include "{inc}"\n' for inc in [*includes, "io.dl"]))
    registry = {
        "inputs": program.inputs,
        "outputs": program.outputs,
        "layers": {
            layer.name: {
                "rank": layer.rank,
                "version": layer.version,
                "source": layer.source,
                "namespace": layer.namespace,
                "target_namespace": layer.target_namespace,
                "requires": layer.requires,
                "open": sorted(layer.open),
                "exports": sorted(layer.exports),
                "extends": sorted(layer.extends),
                "accepts": sorted(layer.accepts),
                "user": sorted(layer.user),
                "driver": sorted(layer.driver),
                "outputs": sorted(layer.outputs),
                "probes": [p.kind for p in layer.probes],
                "providers": [p.name for p in layer.providers],
                "qualified": layer.qualified,
            }
            for layer in layers
        },
    }
    (outdir / "registry.json").write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
    # The gated-recursion check is pure Python and independent of Soufflé's
    # checks, so it runs while Soufflé starts; it is reported after them.
    with ThreadPoolExecutor(max_workers=1) as pool:
        gated = pool.submit(check_gated_recursion, layers, layer_cache)
        program.errors = (composed_check or _check_composed)(
            build,
            layers,
            [
                outdir / "io.dl",
                *executable_files,
                *([declarations] if declarations is not None else []),
            ],
        )
        # After Soufflé's checks: a request clause reading needed through a cycle
        # is reported there, by the stratification error that names the rules.
        if not program.errors:
            program.errors = _check_decision_relations(layers)
        if not program.errors:
            program.errors = _check_discovery_checks(layers)
    if not program.errors:
        program.errors = gated.result()
    if not program.errors:
        program.build = build
    return program


def stratum_relations(program: Path) -> set[str]:
    return set(re.findall(r"^\s*\.decl\s+([A-Za-z_][\w.]*)", program.read_text(), re.MULTILINE))


def _check_strata(layers: list[Layer]) -> list[str]:
    """The one-way rule of build-time strata: scan results and what a stratum
    derives from them never re-enter the configure model, so no configure-time
    layer (or the project's rules) may mention a relation a stratum declares."""
    errors: list[str] = []
    for owner in layers:
        for st in owner.strata:
            if not st.program.is_file():
                errors.append(f"{owner.name}: stratum {st.name}: missing program {st.program}")
                continue
            rels = stratum_relations(st.program)
            for layer in layers:
                text = re.sub(r"//[^\n]*", "", layer.text)
                for rel in sorted(rels):
                    if re.search(rf"(?<![\w.]){re.escape(rel)}(?![\w.])", text):
                        errors.append(
                            f"{layer.name}: configure-time rules read {rel}, a relation of "
                            f"build-time stratum {owner.name}:{st.name}; build-time results "
                            "never re-enter the configure model"
                        )
    return errors


def _check_composed(build: Path, layers: list[Layer] = (), reads: list[Path] = ()) -> list[str]:
    """Soufflé's semantic checks on the whole program, without evaluating it.

    Per-layer checks cannot see cross-package cycles, e.g. a package deriving
    `target` from the needed closure, which core's `$Defined` negates."""
    cp = souffle_cache.run(
        ["-w", "--show=type-analysis", str(build)], [build, *reads], build.parent, keep_stdout=False
    )
    if cp.returncode == 0:
        return []
    return composed_errors(cp.stdout, cp.stderr, layers)


def composed_errors(stdout: str, stderr: str, layers: list[Layer]) -> list[str]:
    """Translate a composed check's diagnostics, including --serve startup failures."""
    lines = [line for line in stderr.splitlines() if line.startswith("Error:")]
    text = stdout + stderr
    cycle = re.search(r"Unable to stratify relation\(s\) \{([^}]*)\}", text)
    if cycle:
        return [_stratification_error(set(cycle.group(1).split(",")), layers)]
    return [f"composed program: {line.removeprefix('Error: ')}" for line in lines] or [
        f"composed program rejected by Soufflé:\n{text}"
    ]


_ATOM = re.compile(r"(!?)\s*([A-Za-z_][\w.]*)\s*\(")


def _cycle_rules(scc: set[str], layers: list[Layer]) -> list[tuple[str, str, list[str]]]:
    """(file:line, head, cycle literals of the body) of each clause whose head
    is in the cycle and whose body reads a relation in it, from the most
    user-facing layers that have one: the project's, else search-path rule
    packages', else plugins'. Core is never blamed."""

    from .provenance.program import _mask, _statements

    order = {"project": 0, "path": 1}
    found: list[tuple[str, str, list[str]]] = []
    tier = None
    for layer in sorted(layers, key=lambda la: order.get(la.source, 2)):
        if layer.source == "core":
            continue
        if found and order.get(layer.source, 2) != tier:
            break
        tier = order.get(layer.source, 2)
        for src, dst in zip(layer.files, layer.program_files, strict=False):
            try:
                code = _mask(dst.read_text())
            except OSError:
                continue
            for start, end, kind in _statements(code):
                clause = code[start:end]
                if kind != "clause" or ":-" not in clause:
                    continue
                head, body = clause.split(":-", 1)
                heads = [m.group(2) for m in _ATOM.finditer(head) if m.group(2) in scc]
                reads = [
                    f"{m.group(1)}{m.group(2)}" for m in _ATOM.finditer(body) if m.group(2) in scc
                ]
                if heads and reads:
                    line = code.count("\n", 0, start) + 1
                    rel = os.path.relpath(src)
                    shown = src if rel.startswith("..") else rel
                    found.append((f"{shown}:{line}", heads[0], list(dict.fromkeys(reads))))
    return found


def _stratification_error(scc: set[str], layers: list[Layer]) -> str:
    """Name the authored rules of an unstratifiable cycle: Soufflé names one
    negation somewhere in the cycle, usually inside core."""
    rules = _cycle_rules(scc, layers)
    authored = {head for _, head, _ in rules}
    msg = "composed program: a cycle through negation cannot be stratified"
    if rules:
        msg += f"; it runs through {', '.join(sorted(authored))}:"
        for where, head, reads in rules[:8]:
            msg += f"\n  {where}: {head} reads {', '.join(reads)}"
        if len(rules) > 8:
            msg += f"\n  ... and {len(rules) - 8} more"
    else:
        msg += f" (relations {', '.join(sorted(scc)[:12])}{', ...' if len(scc) > 12 else ''})"
    if "needed" in scc and ("request" in scc or "request_all" in scc):
        msg += (
            "\n  needed is derived from request with negation, so request and request_all "
            "may not depend on needed (or on per-configuration relations such as "
            "config_attr and cond_holds). Derive them from declaration-level facts, "
            "or state the configuration dependence with request_when, dep_when or prop_when."
        )
    elif "needed" in scc:
        msg += (
            "\n  A relation that decides what is built (target, dep, request) may not "
            "depend on needed or on relations derived from it; state the configuration "
            "dependence with a condition (dep_when, prop_when) instead."
        )
    return msg


def check_layers(
    packages: list[PackageRef | Path], work: Path, required_outputs: tuple[str, ...] = ()
) -> list[str]:
    return compose_program(packages, work / "program", work / "ast", required_outputs).errors
