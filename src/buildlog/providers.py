# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded package providers and guarded environmental path resolution.

A plugin registers a format-owning Python object with ``[[provider]]``.
Before inference, ``enumerate_files(kind, root, observations)`` catalogs
its finite layouts within declared prefixes, search directories and files.
``metadata_links(file, observations)`` exposes includes and rule pointers.
All inspected files, directory memberships and negative lookups belong to
the observation certificate. Invalid referenced executable code is fatal;
ordinary metadata errors can remain deferred until binding is demanded.

``decode(res, vocabulary, namespace)`` states immutable available metadata
through ``Result``. Its typed Vocabulary contains static platform names
and constraints, never probe answers. Finder offers retain each format's
``find`` naming and layout rules; cataloging a file does not offer every
alias it contains. The core selects bindings and relays target facts per
platform, independently of the executable union prepared beforehand.

Rule modules are composed once. Their declarations require binding and
their effects require use; visibility alone neither runs their actions
nor adds them to the final lock. No package find/import probe changes code.
Paths that depend on a query-time sysroot remain typed expressions;
``metadata_path`` resolves them as guarded environmental requests, with
ordered negative watches, without decoding metadata or admitting code.

``Result`` preserves file/line origins, symbolic conditions, metadata
includes, versions, attributes, build properties and linkage-dependent
edges. Direct concrete import helpers remain available for format tests;
production uses ``decode_file`` and its ``av_pkg_*`` rows.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .available import Certificate, digest
from .errors import LoaderError

# ---- conditions -------------------------------------------------------------

Cond = tuple
TRUE: Cond = ("true",)
FALSE: Cond = ("false",)


def eq(key: str, value: str) -> Cond:
    return ("eq", key, value)


def defined(target: str) -> Cond:
    return ("defined", target)


def not_(x: Cond) -> Cond:
    if x == TRUE:
        return FALSE
    if x == FALSE:
        return TRUE
    if x[0] == "not":
        return x[1]
    return ("not", x)


def and_(*xs: Cond) -> Cond:
    out = TRUE
    for x in xs:
        if x == FALSE or out == FALSE:
            return FALSE
        if x == TRUE:
            continue
        out = x if out == TRUE else ("and", out, x)
    return out


def or_(*xs: Cond) -> Cond:
    out = FALSE
    for x in xs:
        if x == TRUE or out == TRUE:
            return TRUE
        if x == FALSE:
            continue
        out = x if out == FALSE else ("or", out, x)
    return out


def _sym(text: str) -> str:
    # Written like spec conditions (`$Eq(native.opt, debug)`): bare symbols,
    # so only text without separators survives the tab-separated facts file.
    if not text or any(ch in text for ch in '"\\,()$ \t\n'):
        raise ValueError(f"cannot represent {text!r} in a condition")
    return text


def render(c: Cond) -> str:
    """Soufflé fact syntax of the core `Cond` ADT."""
    tag = c[0]
    if tag == "eq":
        return f"$Eq({_sym(c[1])}, {_sym(c[2])})"
    if tag == "defined":
        return f"$Defined({_sym(c[1])})"
    if tag == "not":
        return f"$Not({render(c[1])})"
    if tag in ("and", "or"):
        return f"${tag.capitalize()}({render(c[1])}, {render(c[2])})"
    raise ValueError(f"constant condition {c!r} has no ADT form")


def show(c: Cond) -> str:
    """Canonical readable text; also the condition's name."""
    tag = c[0]
    if tag in ("true", "false"):
        return tag
    if tag == "eq":
        return f"{c[1]}={c[2]}"
    if tag == "defined":
        return f"defined({c[1]})"
    if tag == "not":
        return f"!{show(c[1])}"
    return f"({show(c[1])} {'&' if tag == 'and' else '|'} {show(c[2])})"


RELATIONS = (
    "res_pkg_import",
    "res_pkg_alias",
    "res_pkg_error",
    "res_pkg_target",
    "res_pkg_target_error",
    "res_pkg_condition",
    "res_pkg_dep",
    "res_pkg_runtime_dep",
    "res_pkg_prop",
    "res_pkg_location",
    "res_pkg_link",
    "res_pkg_whole_archive",
    "res_pkg_rule_package",
    "res_pkg_bare_dep",
    "res_pkg_export",
    "res_pkg_file",
    "res_pkg_version",
    "res_pkg_package",
    "res_pkg_attr",
    "res_pkg_build_prop",
    "res_pkg_linkage_dep",
    "res_pkg_expression",
)


class MetadataError(Exception):
    """A file-level rejection at a line of the metadata file."""

    def __init__(self, line: int | None, message: str):
        super().__init__(message)
        self.line = line
        self.message = message


@dataclass(frozen=True)
class MetadataLinks:
    """Static dependencies of one metadata file, including deferred parse errors."""

    includes: tuple[tuple[str, Path, str], ...] = ()
    rules: tuple[Path, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class Vocabulary:
    """Unconditional platform vocabulary, never inventory answers."""

    os: frozenset[str]
    cpu: frozenset[str]


def static_vocabulary(layers) -> Vocabulary:
    """Read the checked expanded clauses, without evaluating another program."""
    domains: dict[str, set[str]] = {"os": set(), "cpu": set()}
    literal = r'"(?:[^"\\]|\\.)*"'
    pattern = re.compile(rf"platform_vocabulary\(\s*({literal})\s*,\s*({literal})\s*\)\s*\.?")
    for layer in layers:
        for head, body, text in layer.info.clauses:
            if head != "platform_vocabulary":
                continue
            match = pattern.fullmatch(text.strip())
            if body or match is None:
                raise LoaderError(
                    [
                        (
                            f"{layer.manifest}: platform_vocabulary must be unconditional literal facts; "
                            "metadata decoding cannot depend on query-time answers"
                        )
                    ]
                )
            key, value = (json.loads(s) for s in match.groups())
            if key in domains:
                domains[key].add(value)
    return Vocabulary(frozenset(domains["os"]), frozenset(domains["cpu"]))


def under_sysroot(root: str, path: str) -> str:
    if root and path.startswith("/") and path != root and not path.startswith(root + "/"):
        return root + path
    return path


def _path_value(kind: str, root: str, data: str, is_file=Path.is_file) -> tuple[str, str]:
    args = json.loads(data)
    if kind == "under":
        return "value", under_sysroot(root, args)
    if kind == "rpath":
        path, system = args
        if root and path.startswith(root + "/"):
            path = path[len(root) :]
        return ("skip", "") if re.match(system, path) else ("value", f"-Wl,-rpath,{path}")
    if kind == "library":
        name, directories, suffixes = args
        for directory in directories:
            for suffix in suffixes:
                path = Path(under_sysroot(root, str(Path(directory) / f"lib{name}{suffix}")))
                if is_file(path):
                    return "link", os.path.normpath(str(path))
        return "flag", f"-l{name}"
    raise ValueError(f"unknown metadata path expression {kind!r}")


def metadata_path(ctx, req_id: str, args: tuple[str, ...], results) -> dict[str, list]:
    """Resolve paths only; no metadata reads or extension of certified scope."""
    from .discovery import watch_identity

    kind, root, data = args
    watches = []

    def is_file(path: Path) -> bool:
        watches.append(watch_identity(str(path)))
        return path.is_file()

    status, value = _path_value(kind, root, data, is_file)
    row = (req_id, status, value)
    return {
        "res_metadata_path": [row],
        "__watch__": watches,
        "__origin__": [("res_metadata_path", row, value)] if status == "link" else [],
    }


def metadata_path_fingerprint(args: tuple[str, ...], results) -> str:
    return digest([1, args])


@dataclass
class Result:
    req: str
    file: Path
    rows: dict[str, list[tuple[str, ...]]] = field(
        default_factory=lambda: {rel: [] for rel in RELATIONS}
    )
    origins: list[tuple[str, tuple[str, ...], str]] = field(default_factory=list)
    conditions: dict[str, Cond] = field(default_factory=dict)
    failed: bool = False
    # The file an origin line refers to (a CMake per-config file, a CPS
    # supplement); defaults to the imported file.
    current: Path | None = None
    # Where the file's target platform's files are on this machine ("" when
    # the file is not in one): a path under it is a path on the target.
    sysroot: str = ""
    observations: Certificate | None = None
    platform_guards: list[tuple[str, str, str, str]] = field(default_factory=list)
    platform_origins: list[tuple[str, tuple[str, ...], str]] = field(default_factory=list)
    symbolic_paths: bool = False

    def path_expression(self, kind: str, args, line: int | None = None) -> str:
        data = json.dumps(args, sort_keys=True, separators=(",", ":"))
        token = "path:" + digest([self.req, kind, data])
        assert self.observations is not None
        status, value = _path_value(kind, self.sysroot, data, self.observations.is_file)
        self.add("res_pkg_expression", token, kind, data, status, value, line=line)
        return token

    def rpath(self, t: str, path: str, system: str, c: Cond, line: int | None) -> None:
        if self.symbolic_paths:
            self.prop(
                t, "native.link_flag", self.path_expression("rpath", [path, system], line), c, line
            )
        else:
            status, value = _path_value("rpath", self.sysroot, json.dumps([path, system]))
            if status != "skip":
                self.prop(t, "native.link_flag", value, c, line)

    def on_target(self, path: str) -> str:
        """The path as the target sees it: a path under the sysroot loses the
        sysroot prefix (pkg-config's PKG_CONFIG_SYSROOT_DIR, inverted)."""
        root = self.sysroot
        if root and path.startswith(root + "/"):
            return path[len(root) :]
        return path

    def under_sysroot(self, path: str) -> str:
        """An absolute path of the target's file system as this machine sees
        it (what pkg-config does with PKG_CONFIG_SYSROOT_DIR), unless it is
        there already."""
        return (
            self.path_expression("under", path)
            if self.symbolic_paths
            else under_sysroot(self.sysroot, path)
        )

    def origin(self, line: int | None) -> str:
        f = self.current or self.file
        return f"{f}:{line}" if line else str(f)

    def add(self, rel: str, *values: str, line: int | None = None) -> None:
        row = (self.req, *values)
        if row not in self.rows[rel]:
            self.rows[rel].append(row)
        entry = (rel, row, self.origin(line))
        if entry not in self.origins:
            self.origins.append(entry)

    def cond(self, c: Cond, line: int | None) -> str | None:
        """Condition name for a row ("" is unconditional); None drops the row."""
        if c == TRUE:
            return ""
        if c == FALSE:
            return None
        name = f"import:{show(c)}"
        if name not in self.conditions:
            self.conditions[name] = c
        self.add("res_pkg_condition", name, render(c), line=line)
        return name

    def target(self, t: str, kind: str, line: int | None) -> None:
        self.add("res_pkg_target", t, kind, line=line)

    def dep(self, t: str, d: str, vis: str, c: Cond, line: int | None) -> None:
        n = self.cond(c, line)
        if n is not None:
            self.add("res_pkg_dep", t, d, vis, n, line=line)

    def runtime_dep(self, t: str, m: str, place: str, c: Cond, line: int | None) -> None:
        """t loads module m at run time (L1's runtime_dep: required, never
        linked, placed by `place`)."""
        n = self.cond(c, line)
        if n is not None:
            self.add("res_pkg_runtime_dep", t, m, place, n, line=line)

    def bare_dep(self, t: str, name: str, vis: str, c: Cond, line: int | None) -> None:
        """An edge to a foreign bare name of a namespaced import (L3_import
        resolves it against the names namespaced imports export)."""
        n = self.cond(c, line)
        if n is not None:
            self.add("res_pkg_bare_dep", t, name, vis, n, line=line)

    def export(self, name: str, t: str, line: int | None) -> None:
        """A namespaced import defines target t for the file's own name."""
        self.add("res_pkg_export", name, t, line=line)

    def prop(self, t: str, key: str, value: str, c: Cond, line: int | None) -> None:
        n = self.cond(c, line)
        if n is not None:
            self.add("res_pkg_prop", t, key, value, n, line=line)

    def location(self, t: str, path: str, c: Cond, line: int | None) -> None:
        n = self.cond(c, line)
        if n is not None:
            self.add("res_pkg_location", t, path, n, line=line)

    def link(self, t: str, path: str, c: Cond, line: int | None) -> None:
        n = self.cond(c, line)
        if n is not None:
            self.add("res_pkg_link", t, path, n, line=line)

    def whole_archive(self, t: str, lib: str, c: Cond, line: int | None) -> None:
        n = self.cond(c, line)
        if n is not None:
            self.add("res_pkg_whole_archive", t, lib, n, line=line)

    def alias(self, name: str, to: str, line: int | None) -> None:
        """The metadata itself says `name` stands for target `to`."""
        self.add("res_pkg_alias", name, to, line=line)

    def include(self, provider: str, path: str, namespace: str, line: int | None) -> None:
        """Another file to import with `provider` (the metadata includes it),
        under `namespace` ("" for none)."""
        self.add("res_pkg_file", provider, path, namespace, line=line)

    def version(self, t: str, major: str, minor: str, patch: str, line: int | None) -> None:
        self.add("res_pkg_version", t, major, minor, patch, line=line)

    def version_string(self, t: str, version: str, line: int | None) -> None:
        """A dotted version as res_pkg_version's three fields: `2.0` is
        2.0.0, and anything past the second dot stays in the patch field
        (`1.2.3.4` is 1, 2, 3.4), so the fields rejoin to the version."""
        parts = version.split(".", 2)
        parts += ["0"] * (3 - len(parts))
        self.version(t, parts[0], parts[1], parts[2], line)

    def package(self, t: str, pkg: str, comp: str, line: int | None) -> None:
        """t belongs to the package the metadata names `pkg` (its own name,
        as a consumer finds it), as component `comp` ("" for the package as
        a whole: its default components, or a format without components)."""
        self.add("res_pkg_package", t, pkg, comp, line=line)

    def attr(self, t: str, key: str, value: str, line: int | None) -> None:
        """Metadata about t that is not a usage requirement (keys belong to
        the provider)."""
        self.add("res_pkg_attr", t, key, value, line=line)

    def build_prop(self, t: str, key: str, value: str, line: int | None) -> None:
        """How imported target t was built (a private prop: consumers are
        checked against it, never given it)."""
        self.add("res_pkg_build_prop", t, key, value, line=line)

    def linkage_dep(self, pkg: str, t: str, linkage: str, d: str, line: int | None) -> None:
        """t depends on d if package pkg is linked `linkage` (static | shared);
        the level above chooses (package_linkage)."""
        self.add("res_pkg_linkage_dep", pkg, t, linkage, d, line=line)

    def rule_package(self, path: str, line: int | None) -> None:
        self.add("res_pkg_rule_package", path, line=line)

    def error(self, message: str, line: int | None) -> None:
        self.failed = True
        self.add("res_pkg_error", f"{self.origin(line)}: {message}", line=line)

    def target_error(self, t: str, message: str, line: int | None) -> None:
        """Unsupported metadata of one target: an error only if t is needed."""
        self.add("res_pkg_target_error", t, f"{self.origin(line)}: {t}: {message}", line=line)

    def require_platform(self, t: str, condition: Cond, message: str, line: int | None) -> None:
        if condition == TRUE:
            return
        if condition == FALSE:
            self.target_error(t, message, line)
            return
        name = self.cond(condition, line)
        assert name is not None
        row = (self.req, t, name, f"{self.origin(line)}: {t}: {message}")
        self.platform_guards.append(row)
        self.platform_origins.append(("av_pkg_platform", row, self.origin(line)))

    def finish(self) -> dict[str, list]:
        self.rows["res_pkg_import"] = [(self.req, "error" if self.failed else "ok")]
        out: dict[str, list] = dict(self.rows)
        out["__origin__"] = list(self.origins)
        return out

    def rejected(self) -> dict[str, list]:
        """A file-level rejection keeps no partial facts, only the error."""
        errors = self.rows["res_pkg_error"]
        keep = {row for row in errors}
        self.rows = {rel: [] for rel in RELATIONS}
        self.rows["res_pkg_error"] = errors
        self.origins = [o for o in self.origins if o[1] in keep]
        self.failed = True
        return self.finish()


def stat_token(path: Path) -> list:
    try:
        st = path.stat()
    except OSError:
        return [str(path), None]
    return [str(path), st.st_mtime_ns, st.st_size]


# ---- registry ---------------------------------------------------------------


@dataclass(frozen=True)
class Registered:
    name: str
    entry: str
    rules: Path | None


_registry: dict[str, Registered] = {}
_objects: dict[str, object] = {}


def activate(decls) -> None:
    """Register the providers of the composed program (ProviderDecl list)."""
    _registry.clear()
    for d in decls:
        _registry[d.name] = Registered(d.name, d.entry, d.rules)


def registered(name: str) -> bool:
    return name in _registry


def provider(name: str):
    reg = _registry[name]
    obj = _objects.get(reg.entry)
    if obj is None:
        mod, _, attr = reg.entry.partition(":")
        obj = getattr(importlib.import_module(mod), attr)
        _objects[reg.entry] = obj
    return obj


def _rules(name: str) -> str | None:
    rules = _registry[name].rules
    return str(rules) if rules is not None else None


# ---- the two core probes ----------------------------------------------------


# The shape of pkg_find's rows; part of its cache key.
FIND_ROWS = "res_pkg_find/6"


def pkg_find(ctx, req_id: str, args: tuple[str, ...], results) -> dict[str, list]:
    name, prov, kind, root = args
    files = provider(prov).find(name, kind, Path(root)) if Path(root).is_dir() else []
    candidates = [(req_id, name, prov, str(f)) for f in sorted(set(map(str, files)))]
    # A provider that finds a name only as an alias the file states (not an
    # id it defines) says so; a defining file beats it (L3_discovery.dl).
    by_alias = getattr(provider(prov), "by_alias", None)
    aliased = candidates if by_alias is not None and by_alias(name) else []
    # A file that declares itself the same package as another file (a
    # sibling the provider verified) is one candidate with it, not two
    # (L3_discovery.dl); a neutral row, any provider may state it.
    same_hook = getattr(provider(prov), "same_as", None)
    same: list[tuple[str, ...]] = []
    invalid: list[tuple[str, ...]] = []
    for _, _, _, file in candidates if same_hook is not None else []:
        try:
            same += [(req_id, name, prov, file, str(o)) for o in same_hook(Path(file))]
        except MetadataError as exc:
            invalid.append((req_id, name, prov, file, exc.message))
    # The root comes back with the result: core counts a candidate for every
    # build platform whose roots include it (L3_discovery.dl).
    return {
        "res_pkg_find": [(req_id, name, prov, kind, root, str(len(candidates)))],
        "res_pkg_candidate": candidates,
        "res_pkg_candidate_alias": aliased,
        "res_pkg_candidate_same": same,
        "res_pkg_candidate_invalid": invalid,
        "__origin__": [("res_pkg_candidate", row, row[3]) for row in candidates]
        + [("res_pkg_candidate_alias", row, row[3]) for row in aliased]
        + [("res_pkg_candidate_same", row, row[3]) for row in same]
        + [("res_pkg_candidate_invalid", row, row[3]) for row in invalid],
    }


def pkg_import(ctx, req_id: str, args: tuple[str, ...], results) -> dict[str, list]:
    prov, file, namespace, sysroot = args
    out = load_file(provider(prov), req_id, file, namespace, results, sysroot)
    rules = _rules(prov)
    if rules is not None:
        # The provider's own rules come with every file it loads, so they
        # stay loaded exactly as long as some import request reaches it.
        row = (req_id, rules)
        out["res_pkg_rule_package"] = [*out["res_pkg_rule_package"], row]
        out["__origin__"].append(("res_pkg_rule_package", row, rules))
    return out


def load_file(
    obj, req_id: str, file: str, namespace: str, results, sysroot: str = ""
) -> dict[str, list]:
    """The rows of one import of `file` by provider object `obj`."""
    res = Result(req_id, Path(file), sysroot=sysroot)
    try:
        obj.load(res, results, namespace)
    except MetadataError as exc:
        res.error(exc.message, exc.line)
        return res.rejected()
    except OSError as exc:
        res.error(f"cannot read: {exc.strerror or exc}", None)
        return res.rejected()
    return res.finish()


def decode_file(
    obj,
    bundle: str,
    file: Path,
    namespace: str,
    vocabulary: Vocabulary,
    observations: Certificate,
    sysroot: str = "",
    *,
    symbolic_paths: bool = False,
) -> dict[str, list]:
    """Decode immutable available data; this API cannot receive probe answers."""
    if not isinstance(vocabulary, Vocabulary):
        raise TypeError("available decoding requires a static Vocabulary, not probe answers")
    observations.file(file)
    links = obj.metadata_links(file, observations)
    res = Result(
        bundle, file, sysroot=sysroot, observations=observations, symbolic_paths=symbolic_paths
    )
    try:
        obj.decode(res, vocabulary, namespace)
    except MetadataError as exc:
        res.error(exc.message, exc.line)
    except OSError as exc:
        res.error(f"cannot read: {exc.strerror or exc}", None)
    named = {Path(p).resolve() for _, p in res.rows["res_pkg_rule_package"]}
    allowed = {p.resolve() for p in links.rules}
    if not named <= allowed:
        raise MetadataError(
            None, f"{file}: decoder named out-of-bound rule packages {named - allowed}"
        )
    included = {(f, Path(os.path.abspath(p)), ns) for _, f, p, ns in res.rows["res_pkg_file"]}
    bounded = {(f, Path(os.path.abspath(p)), ns) for f, p, ns in links.includes}
    if not included <= bounded:
        raise MetadataError(
            None, f"{file}: decoder named out-of-bound metadata includes {included - bounded}"
        )
    # Available rows are not current package-probe answers.
    out = res.rejected() if res.failed else res.finish()
    return {
        ("av_pkg_" + rel.removeprefix("res_pkg_")): rows
        for rel, rows in out.items()
        if rel != "__origin__"
    } | {
        "av_pkg_platform": [] if res.failed else res.platform_guards,
        "__origin__": [
            ("av_pkg_" + rel.removeprefix("res_pkg_"), row, origin)
            for rel, row, origin in out["__origin__"]
        ]
        + ([] if res.failed else res.platform_origins),
    }


def _digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def find_fingerprint(args: tuple[str, ...], results) -> str:
    name, prov, kind, root = args
    watched = provider(prov).find_watched(name, kind, Path(root))
    return _digest([FIND_ROWS, _registry[prov].entry, _rules(prov), watched])


def import_fingerprint(args: tuple[str, ...], results) -> str:
    prov, file, namespace, sysroot = args
    obj = provider(prov)
    # A provider reads a sysroot's files only when the file is in one.
    watched = (
        obj.watched(Path(file), results, sysroot) if sysroot else obj.watched(Path(file), results)
    )
    return _digest([_registry[prov].entry, namespace, sysroot, watched])
