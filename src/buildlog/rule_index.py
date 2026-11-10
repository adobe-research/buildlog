# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Finite dependency preparation, without inference or package activation epochs.

Providers enumerate their supported layouts and expose metadata includes and rule
pointers. Every read is covered by the returned observation certificate. Executable
packages form a strict union; this module never chooses a conflict winner.
"""

from __future__ import annotations

import importlib
import os
import pickle
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import modules, providers
from .available import Certificate, File, digest
from .errors import LoaderError
from .facts import (
    default_vars,
    project_dl_fact_rows,
    project_spec,
    read_project_config,
    source_projects,
    spec_relations,
    substitute_vars,
    user_file_parts,
)
from .layers import package_files, parse_manifest
from .packages import Candidate, Package, resolve_packages

_ROOTS = {
    "package_prefix": (None, 0, "prefix", ("prefix",)),
    "package_prefix_on": (None, 1, "prefix", ("platform", "prefix")),
    "package_search_dir": (0, 1, "dir", ("provider", "dir")),
    "package_search_dir_on": (1, 2, "dir", ("platform", "provider", "dir")),
    "package_file": (0, 1, "file", ("provider", "path")),
    "package_file_on": (1, 2, "file", ("platform", "provider", "path")),
    "package_namespace": (None, 0, "namespace", ("path", "ns")),
    "package_pin": (None, 1, "pin", ("name", "file")),
    "package_pin_on": (None, 2, "pin", ("platform", "name", "file")),
}


@dataclass(frozen=True)
class Root:
    provider: str | None
    kind: str
    path: Path
    origin: str


@dataclass
class Prepared:
    packages: list[Package]
    files: tuple[File, ...]
    certificate: Certificate
    code_digest: str
    deferred_errors: dict[File, str]
    explicit_manifests: frozenset[Path]
    discovered: tuple[tuple[Path, str], ...]
    roots: tuple[Root, ...]
    metadata_packages: tuple[Package, ...] = ()
    path_spellings: tuple[tuple[str, str], ...] = ()
    cached: bool = field(default=False, compare=False)


def _rows(value: object, columns: tuple[str, ...], origin: str) -> list[list[str]]:
    arity = len(columns)
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        grouped = []
        for key, body in value.items():
            if isinstance(body, dict):
                if set(body) != set(columns[1:]):
                    raise LoaderError([f"{origin}: expected keys {', '.join(columns[1:])}"])
                grouped.append([key, *(body[col] for col in columns[1:])])
            else:
                grouped.append([key, body])
        value = grouped
    out = []
    for row in value if isinstance(value, list) else [value]:
        if isinstance(row, str):
            row = [row]
        elif isinstance(row, dict) and set(row) == set(columns):
            row = [row[col] for col in columns]
        if (
            not isinstance(row, list)
            or len(row) != arity
            or not all(isinstance(v, str) for v in row)
        ):
            raise LoaderError([f"{origin}: expected {arity} string columns, got {row!r}"])
        out.append(row)
    return out


def declared_roots(
    project: Path,
    user_files: list[Path],
    overrides: dict[str, str] | None,
    certificate: Certificate,
    *,
    namespaces: dict[Path, set[str]] | None = None,
    spellings: dict[str, str] | None = None,
) -> list[Root]:
    namespaces = {} if namespaces is None else namespaces
    spellings = {} if spellings is None else spellings
    certificate.file(project / "buildlog.toml")
    certificate.file(project / "pyproject.toml")
    spec = project_spec(project)
    for path in spec.inputs:
        certificate.file(path)
    variables = default_vars(project, overrides)
    inputs = [(p, ()) for p in spec.tomls]
    if spec.config.name == "pyproject.toml":
        inputs.append((spec.config, ("tool", "buildlog")))
    inputs += [user_file_parts(p) for p in user_files]
    found: list[tuple[str, list[str], Path]] = []
    for path, table in inputs:
        content = certificate.file(path)
        if content is None:
            raise LoaderError([f"{path}: missing preparation input"])
        if path.suffix == ".toml":
            try:
                data = tomllib.loads(content.decode())
            except (UnicodeError, tomllib.TOMLDecodeError) as exc:
                raise LoaderError([f"{path}: {exc}"]) from exc
            for key in table:
                data = data.get(key, {})
            if not isinstance(data, dict):
                raise LoaderError([f"{path}: no table {'.'.join(table)}"])
            # Presets not selected by the invoker are not part of this scope.
            for rel, (_, _, _, columns) in _ROOTS.items():
                if rel in data:
                    found += [(rel, r, path) for r in _rows(data[rel], columns, f"{path}: {rel}")]
        elif path.suffix == ".facts" and path.stem in _ROOTS:
            found += [
                (path.stem, ln.split("\t"), path) for ln in content.decode().splitlines() if ln
            ]
        elif path.suffix == ".dl":
            rows, _ = project_dl_fact_rows([path])
            found += [(rel, list(row), path) for rel in _ROOTS for row in rows.get(rel, ())]
    for path in [*spec.facts, *spec.dls]:
        if path.suffix == ".facts" and path.stem in _ROOTS:
            content = certificate.file(path)
            found += [
                (path.stem, ln.split("\t"), path) for ln in content.decode().splitlines() if ln
            ]
        elif path.suffix == ".dl":
            rows, _ = project_dl_fact_rows([path])
            found += [(rel, list(row), path) for rel in _ROOTS for row in rows.get(rel, ())]
    out = []
    for rel, row, path in found:
        prov_at, path_at, kind, columns = _ROOTS[rel]
        row = _rows([row], columns, f"{path}: {rel}")[0]
        row = [substitute_vars(v, variables, str(path)) for v in row]
        root_path = Path(row[path_at]).expanduser().absolute()
        spellings[row[path_at]] = str(root_path)
        if kind == "namespace":
            namespaces.setdefault(root_path, set()).add(row[1])
            continue
        if kind == "pin":
            continue
        out.append(
            Root(
                row[prov_at] if prov_at is not None else None,
                kind,
                root_path,
                f"{path}: {rel}",
            )
        )
    for _, directory, _ in source_projects(user_files, variables):
        out += declared_roots(
            directory, [], None, certificate, namespaces=namespaces, spellings=spellings
        )
    return out


def prepare(
    project: Path,
    user_files: list[Path],
    overrides: dict[str, str] | None,
    packages: list[Package],
    prefixes: list[Path],
) -> Prepared:
    """Prepare a bounded union. Does not compose, activate modules, or run probes."""
    project = project.resolve()
    certificate = Certificate()
    for name in (
        "available.py",
        "facts.py",
        "layers.py",
        "packages.py",
        "providers.py",
        "rule_index.py",
    ):
        certificate.file(Path(__file__).with_name(name))
    namespaces: dict[Path, set[str]] = {}
    spellings: dict[str, str] = {}
    roots = declared_roots(
        project, user_files, overrides, certificate, namespaces=namespaces, spellings=spellings
    )
    initial = packages
    rule_dirs: dict[Path, str] = {}
    metadata_packages: dict[Path, Package] = {}
    seen: set[File] = set()
    waiting: set[File] = set()
    deferred: dict[File, str] = {}
    implementations: dict[str, str] = {}
    for pkg in packages:
        certificate.file(pkg.candidate.manifest)
        if parse_manifest(pkg.candidate.manifest) != pkg.data:
            raise LoaderError([f"{pkg.candidate.manifest}: changed since package resolution"])

    def observe_resolver_roots() -> None:
        for directory in [*(p / "share" / "buildlog" for p in prefixes), project / "packages"]:
            for child in certificate.directory(directory):
                certificate.file(child / "manifest.toml")

    def note_rules(directory: Path, origin: str, *, implicit_metadata: bool = False) -> None:
        directory = directory.absolute()
        manifest = directory / "manifest.toml"
        certificate.file(manifest)
        problems: list[str] = []
        data = parse_manifest(manifest, problems)
        if problems:
            raise LoaderError([f"{origin}: {p}" for p in problems])
        executable = any(
            data.get(key)
            for key in (
                "layers",
                "facts",
                "stratum",
                "probe",
                "provider",
                "requires",
                "open",
                "accepts",
                "user",
                "driver",
                "exports",
                "namespace",
                "namespaces",
            )
        )
        if (
            implicit_metadata
            and not executable
            and manifest.resolve() not in {p.candidate.manifest.resolve() for p in initial}
        ):
            metadata_packages.setdefault(
                directory.resolve(),
                Package(Candidate(data["name"], manifest, "path"), data, origin),
            )
            return
        rule_dirs.setdefault(directory.resolve(), origin)

    while True:
        for pkg in packages:
            if pkg.candidate.source != "path":
                continue
            for key, subject, action in (
                ("probe", "kind", "run probes"),
                ("provider", "name", "provide package discovery"),
            ):
                if declarations := pkg.data[key]:
                    raise LoaderError(
                        [
                            (
                                f"{pkg.name}: rule package found on a search path declares [[{key}]] "
                                f"({', '.join(getattr(d, subject) for d in declarations)}); "
                                "only registered Python plugins (entry point group buildlog.plugins) "
                                f"may {action}"
                            )
                        ]
                    )
        decls = [d for pkg in packages for d in pkg.data["provider"]]
        by_name = {}
        for decl in decls:
            other = by_name.setdefault(decl.name, decl)
            if other.entry != decl.entry or other.rules != decl.rules:
                raise LoaderError(
                    [f"conflicting provider {decl.name}: {other.manifest}, {decl.manifest}"]
                )
        providers.activate(decls)
        pending = list(waiting)
        waiting.clear()
        for pkg in packages:
            certificate.file(pkg.candidate.manifest)
            for entry in pkg.data["imports"]:
                pending.append(
                    File(
                        entry["format"],
                        Path(os.path.abspath(pkg.candidate.manifest.parent / entry["file"])),
                        entry["namespace"],
                    )
                )
        for name, decl in sorted(by_name.items()):
            obj = providers.provider(name)
            if not callable(getattr(obj, "enumerate_files", None)) or not callable(
                getattr(obj, "metadata_links", None)
            ):
                raise LoaderError(
                    [
                        f"{decl.manifest}: provider {name} requires bounded enumerate_files and metadata_links"
                    ]
                )
            # Provider implementations are part of the certificate, independently
            # of the data files they enumerate.
            module = importlib.import_module(decl.entry.partition(":")[0])
            if module.__file__ is None:
                raise LoaderError([f"provider {name}: implementation has no source file"])
            certificate.file(Path(module.__file__))
            implementations[name] = decl.entry
            if decl.rules is not None:
                note_rules(decl.rules, f"provider {name}")
            for root in roots:
                if root.provider is not None and root.provider != name:
                    continue
                files = (
                    [root.path]
                    if root.kind == "file"
                    else obj.enumerate_files(root.kind, root.path, certificate)
                )
                pending.extend(
                    File(name, p.absolute(), ns)
                    for p in files
                    for ns in sorted(namespaces.get(p.absolute(), {""}))
                )
        unknown = {r.provider for r in roots if r.provider is not None} - set(by_name)
        while pending:
            file = pending.pop()
            if file in seen:
                continue
            if not providers.registered(file.provider):
                unknown.add(file.provider)
                waiting.add(file)
                continue
            seen.add(file)
            certificate.file(file.path)
            links = providers.provider(file.provider).metadata_links(file.path, certificate)
            if links.error:
                deferred[file] = links.error
            for directory in links.rules:
                note_rules(
                    directory,
                    f"{file.provider}: {file.path}",
                    implicit_metadata=(
                        file.provider == "buildlog"
                        and file.path.name == "manifest.toml"
                        and directory.resolve() == file.path.parent.resolve()
                    ),
                )
            for name, path, ns in links.includes:
                pending.append(File(name, Path(os.path.abspath(path)), ns))
        discovered = sorted(rule_dirs.items())
        # Resolve against the explicit roots' vocabulary, not dormant accepts.
        observe_resolver_roots()
        config = read_project_config(project_spec(project).config)
        union = resolve_packages(
            project, config.get("uses", []), spec_relations(project), prefixes, discovered
        )
        actual = {p.candidate.manifest.resolve().parent for p in union}
        missing = set(rule_dirs) - actual
        if missing:
            raise LoaderError(
                [
                    f"{p}: rule package has unresolved requirements; cannot omit it from the executable union"
                    for p in sorted(missing)
                ]
            )
        old = {p.candidate.manifest.resolve() for p in packages}
        packages = union
        if old == {p.candidate.manifest.resolve() for p in packages}:
            if unknown:
                raise LoaderError(
                    [f"unknown bounded metadata provider: {n}" for n in sorted(unknown)]
                )
            break
    code = []
    for pkg in packages:
        data = pkg.data
        manifest = pkg.candidate.manifest
        paths = {
            manifest,
            *(manifest.parent / f for f in (*data["layers"], *data["facts"])),
            *(st.program for st in data["stratum"]),
        }
        for path in package_files(manifest, data):
            content = certificate.file(path)
            if content is None:
                raise LoaderError([f"{manifest}: missing package input {path}"])
        code.append(
            [
                str(manifest.resolve()),
                [[str(p), certificate.files[str(p.absolute())]] for p in sorted(paths)],
            ]
        )
    certificate.confirm()
    if not {p.candidate.manifest.resolve() for p in initial} <= {
        p.candidate.manifest.resolve() for p in packages
    }:
        raise LoaderError(["preparation withdrew an explicit rule package"])
    return Prepared(
        packages,
        tuple(sorted(seen)),
        certificate,
        digest([code, implementations]),
        deferred,
        frozenset(p.candidate.manifest.resolve() for p in initial),
        tuple(sorted(rule_dirs.items())),
        tuple(roots),
        tuple(metadata_packages[d] for d in sorted(metadata_packages) if d not in actual),
        tuple(sorted(spellings.items())),
    )


def available_rows(prepared: Prepared, vocabulary: providers.Vocabulary) -> dict[str, list]:
    """Decode bounded data and finder offers without demand or probe answers."""
    declarations = [d for p in prepared.packages for d in p.data["provider"]]
    providers.activate(declarations)
    provider_rules = {d.name: d.rules for d in declarations}
    payloads = {}
    rows: dict[str, set[tuple[str, ...]]] = {}
    origins = set()

    def add(relation: str, row: tuple[str, ...], origin: str) -> None:
        rows.setdefault(relation, set()).add(row)
        origins.add((relation, row, origin))

    for spelling, path in prepared.path_spellings:
        add("av_pkg_spelling", (spelling, path), path)
    for path in {*(str(r.path) for r in prepared.roots), *(str(f.path) for f in prepared.files)}:
        add("av_pkg_spelling", (path, path), path)
        add("av_pkg_spelling", (os.path.relpath(path), path), path)

    for root in prepared.roots:
        if root.kind in {"prefix", "dir"}:
            for name in provider_rules:
                if root.provider in (None, name):
                    add("av_pkg_root", (name, root.kind, str(root.path)), root.origin)

    for package in prepared.metadata_packages:
        manifest = package.candidate.manifest
        module = modules.identity(manifest)
        add("module_manifest", (module, str(manifest.resolve().parent)), str(manifest))
        for entry in package.data["imports"]:
            add(
                "module_import",
                (
                    module,
                    entry["format"],
                    os.path.abspath(manifest.parent / entry["file"]),
                    entry["namespace"],
                ),
                str(manifest),
            )

    for file in prepared.files:
        try:
            payload = providers.decode_file(
                providers.provider(file.provider),
                file.identity,
                file.path,
                file.namespace,
                vocabulary,
                prepared.certificate,
                symbolic_paths=True,
            )
        except providers.MetadataError as exc:
            raise LoaderError([f"{file.path}: {exc}"]) from exc
        payloads[file] = payload
        for relation, values in payload.items():
            if relation != "__origin__":
                rows.setdefault(relation, set()).update(values)
        origins.update(payload["__origin__"])
        add(
            "av_pkg_bundle",
            (file.identity, file.provider, str(file.path), file.namespace, ""),
            str(file.path),
        )
        rules = provider_rules.get(file.provider)
        if rules is not None:
            add("av_pkg_rule_package", (file.identity, str(rules)), str(rules))

    def names_of(root: File) -> frozenset[str]:
        names = set()
        visited = set()
        pending = [root]
        while pending:
            file = pending.pop()
            if file in visited:
                continue
            visited.add(file)
            payload = payloads.get(file)
            if payload is None:
                raise LoaderError([f"{root.path}: out-of-bound included metadata {file}"])
            names.update(t for _, t, _ in payload["av_pkg_target"])
            names.update(n for _, n, _ in payload["av_pkg_alias"])
            names.update(t for _, _, t in payload["av_pkg_alias"])
            pending.extend(
                File(provider, Path(os.path.abspath(path)), namespace)
                for _, provider, path, namespace in payload["av_pkg_file"]
            )
        return frozenset(names)

    for file in prepared.files:
        obj = providers.provider(file.provider)
        if not callable(getattr(obj, "offers", None)):
            raise LoaderError([f"provider {file.provider} requires bounded offers"])
        names = names_of(file)
        for root in prepared.roots:
            if root.kind == "file" or root.provider not in (None, file.provider):
                continue
            offered = obj.offers(file.path, root.kind, root.path, names, prepared.certificate)
            if not offered:
                continue
            same = []
            invalid = ""
            try:
                hook = getattr(obj, "same_as", None)
                same = hook(file.path) if hook is not None else []
            except providers.MetadataError as exc:
                invalid = exc.message
            by_alias = getattr(obj, "by_alias", lambda _: False)
            for name in offered:
                base = (name, file.provider, root.kind, str(root.path), str(file.path))
                add(
                    "av_pkg_offer",
                    (*base, "alias" if by_alias(name) else "defining"),
                    str(file.path),
                )
                for other in same:
                    add("av_pkg_same", (*base, str(other)), str(file.path))
                if invalid:
                    add("av_pkg_invalid", (*base, invalid), str(file.path))
    prepared.certificate.confirm()
    return {relation: sorted(values) for relation, values in rows.items()} | {
        "__origin__": sorted(origins)
    }


def _cached_preparation(
    cache: Path, invocation: str, project: Path, prefixes: list[Path]
) -> Prepared | None:
    try:
        record = pickle.loads(cache.read_bytes())
        held = record["prepared"]
        if (
            record["invocation"] == invocation
            and isinstance(held, Prepared)
            and held.certificate.valid()
        ):
            config = read_project_config(project_spec(project).config)
            current = resolve_packages(
                project,
                config.get("uses", []),
                spec_relations(project),
                prefixes,
                list(held.discovered),
            )
            # Entry points may now name different directories or distributions,
            # even if all the previously observed files still exist unchanged.
            if [(p.candidate, p.data) for p in current] == [
                (p.candidate, p.data) for p in held.packages
            ]:
                held.certificate.confirm()
                providers.activate([d for p in held.packages for d in p.data["provider"]])
                held.cached = True
                return held
    except Exception:  # noqa: BLE001 - any unusable cache requires a fresh traversal
        return None
    return None


def prepare_cached(
    project: Path,
    user_files: list[Path],
    overrides: dict[str, str] | None,
    packages: list[Package],
    prefixes: list[Path],
    cache: Path,
) -> Prepared:
    """Reuse a certified traversal, without rediscovering or decoding metadata."""
    project = project.resolve()
    invocation = digest(
        [
            "prepared:3",
            sys.version,
            str(Path.cwd()),
            str(project),
            [str(p) for p in user_files],
            overrides,
            [str(p) for p in prefixes],
            [
                [
                    str(p.candidate.manifest),
                    p.candidate.source,
                    p.candidate.entry_point,
                    p.candidate.dist,
                    p.candidate.dist_version,
                    p.reason,
                ]
                for p in packages
            ],
            {k: v for k, v in os.environ.items() if k.startswith("BUILDLOG_") or k == "HOME"},
        ]
    )
    if (held := _cached_preparation(cache, invocation, project, prefixes)) is not None:
        return held
    prepared = prepare(project, user_files, overrides, packages, prefixes)
    temporary = None
    try:
        prepared.certificate.confirm()
        blob = pickle.dumps({"invocation": invocation, "prepared": prepared})
        cache.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=cache.parent, prefix=".prepared-")
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
        os.replace(temporary, cache)
    except (OSError, pickle.PickleError):
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return prepared
