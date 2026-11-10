# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Finding packages and the project lockfile.

A project loads the core, the names in `uses` (buildlog.toml), everything
those `requires`, packages whose namespace prefixes a spec relation no loaded
package accepts, and rule packages discovered by probes (the outer loop in
engine.configure_project). Names resolve across two kinds of places:

- tier 1: Python plugins registered in the `buildlog.plugins` entry point
  group, each resolving to its manifest directory;
- tier 2: search roots, `<prefix>/share/buildlog/<name>/manifest.toml` for
  each prefix in BUILDLOG_PACKAGE_PATH and `--package-path`, plus the
  project-local `packages/<name>/`.

A name must resolve to exactly one candidate. Nothing is loaded implicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, entry_points
from importlib.metadata import version as dist_version
from pathlib import Path

from .errors import BuildlogError, LoaderError, read_toml
from .layers import PackageRef, package_files, parse_manifest
from .paths import CORE_RULES

ENTRY_POINT_GROUP = "buildlog.plugins"
LOCKFILE = "buildlog.lock"
PACKAGE_PATH_ENV = "BUILDLOG_PACKAGE_PATH"
CORE_NAME = "buildlog"


@dataclass(frozen=True)
class Candidate:
    name: str
    manifest: Path
    source: str  # core | plugin | path
    entry_point: str | None = None
    dist: str | None = None
    dist_version: str | None = None

    def describe(self) -> str:
        if self.source == "plugin":
            return f"plugin {self.entry_point} (distribution {self.dist}) at {self.manifest.parent}"
        return f"{self.source} {self.manifest.parent}"


@dataclass
class Package:
    candidate: Candidate
    data: dict
    reason: str

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def ref(self) -> PackageRef:
        return PackageRef(self.candidate.manifest, self.candidate.source)

    def content_hash(self) -> str:
        """sha256 over the manifest, layer and facts files (names and contents)."""
        h = hashlib.sha256()
        root = self.candidate.manifest.parent
        for path in sorted(package_files(self.candidate.manifest, self.data)):
            h.update(path.relative_to(root).as_posix().encode() + b"\0")
            data = path.read_bytes() if path.is_file() else b""
            h.update(str(len(data)).encode() + b"\0" + data)
        return "sha256:" + h.hexdigest()

    def lock_entry(self, project: Path) -> dict[str, str]:
        entry = {"name": self.name, "version": str(self.data["version"])}
        entry["source"] = self.candidate.source
        if self.candidate.source == "plugin":
            entry["entry_point"] = str(self.candidate.entry_point)
        elif self.candidate.source == "path":
            entry["path"] = Path(
                os.path.relpath(self.candidate.manifest.parent, project)
            ).as_posix()
        entry["hash"] = self.content_hash()
        entry["reason"] = _project_relative(self.reason, project)
        return entry


def _project_relative(reason: str, project: Path) -> str:
    """A reason with each absolute path it names (a discovered request's id
    holds its manifest's) made relative to the project, as a lock entry's
    `path` is, so the lock does not hold the checkout's location."""
    segments = []
    for segment in reason.split(":"):
        path = segment.lstrip()
        if path.startswith("/"):
            whitespace = segment[: len(segment) - len(path)]
            segment = whitespace + Path(os.path.relpath(path, project)).as_posix()
        segments.append(segment)
    return ":".join(segments)


def search_prefixes(package_paths: list[Path] | None = None) -> list[Path]:
    """BUILDLOG_PACKAGE_PATH (colon-separated) followed by `--package-path` prefixes."""
    env = [Path(p) for p in os.environ.get(PACKAGE_PATH_ENV, "").split(":") if p]
    return [p.expanduser().resolve() for p in [*env, *(package_paths or [])]]


class Resolver:
    def __init__(self, project: Path, prefixes: list[Path]):
        self.project = project
        self.roots = [p / "share" / "buildlog" for p in prefixes] + [project / "packages"]
        self._plugins: dict[str, list] | None = None

    def plugins(self) -> dict[str, list]:
        if self._plugins is None:
            self._plugins = {}
            for ep in entry_points(group=ENTRY_POINT_GROUP):
                self._plugins.setdefault(ep.name, []).append(ep)
        return self._plugins

    def _plugin_candidate(self, ep) -> Candidate:
        where = f"plugin {ep.name} ({ep.value})"
        try:
            target = ep.load()
            target = target() if callable(target) else target
            directory = Path(target)
        except Exception as exc:
            raise LoaderError([f"{where}: cannot resolve entry point: {exc}"]) from exc
        dist = ep.dist
        return Candidate(
            ep.name,
            directory / "manifest.toml",
            "plugin",
            ep.value,
            dist.name if dist else None,
            dist.version if dist else None,
        )

    def candidates(self, name: str) -> list[Candidate]:
        found: list[Candidate] = []
        if name == CORE_NAME:
            found.append(_core_candidate())
        for ep in self.plugins().get(name, []):
            found.append(self._plugin_candidate(ep))
        for root in self.roots:
            manifest = root / name / "manifest.toml"
            if manifest.is_file():
                found.append(Candidate(name, manifest.resolve(), "path"))
        unique: dict[tuple[str, Path], Candidate] = {}
        for c in found:
            unique.setdefault((c.source, c.manifest.resolve()), c)
        return list(unique.values())

    def namespace_candidates(self, rel: str) -> list[Candidate]:
        found: dict[Path, Candidate] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for manifest in sorted(root.glob("*/manifest.toml")):
                try:
                    ns = read_toml(manifest).get("namespace")
                except BuildlogError:
                    continue
                if isinstance(ns, str) and rel.startswith(f"{ns}_"):
                    found.setdefault(
                        manifest.resolve(),
                        Candidate(manifest.parent.name, manifest.resolve(), "path"),
                    )
        return list(found.values())

    def where(self) -> str:
        roots = ", ".join(str(r) for r in self.roots)
        return f"entry point group {ENTRY_POINT_GROUP} and search roots {roots}"


def _load(candidate: Candidate, reason: str) -> tuple[Package | None, list[str]]:
    errors: list[str] = []
    if not candidate.manifest.is_file():
        return None, [f"{candidate.describe()}: no manifest.toml"]
    data = parse_manifest(candidate.manifest, errors)
    if data["name"] != candidate.name:
        errors.append(
            f"{candidate.describe()}: manifest name {data['name']} does not match "
            f"the name {candidate.name} it was found under"
        )
    if candidate.dist_version is not None and data["version"] != candidate.dist_version:
        errors.append(
            f"{candidate.name}: manifest version {data['version']} differs from its "
            f"distribution {candidate.dist} {candidate.dist_version}"
        )
    return Package(candidate, data, reason), errors


def _core_candidate() -> Candidate:
    try:
        version = dist_version(CORE_NAME)
    except PackageNotFoundError:  # running from a source tree without metadata
        version = None
    return Candidate(CORE_NAME, CORE_RULES / "manifest.toml", "core", None, CORE_NAME, version)


def resolve_packages(
    project: Path,
    uses: list[str],
    spec_relations: dict[str, str],
    prefixes: list[Path],
    discovered: list[tuple[Path, str]] = (),
) -> list[Package]:
    """Resolve the package set; raise LoaderError listing every problem."""
    resolver = Resolver(project, prefixes)
    loaded: dict[str, Package] = {}
    errors: list[str] = []

    def add(candidate: Candidate, reason: str) -> None:
        pkg, errs = _load(candidate, reason)
        errors.extend(errs)
        if pkg is None:
            return
        other = loaded.get(pkg.name)
        if other is not None:
            if other.candidate.manifest.resolve() != candidate.manifest.resolve():
                errors.append(
                    f"{pkg.name}: found twice ({other.candidate.describe()} for {other.reason}; "
                    f"{candidate.describe()} for {reason})"
                )
            return
        loaded[pkg.name] = pkg

    def by_name(name: str, reason: str) -> None:
        if name in loaded:
            return
        found = resolver.candidates(name)
        if not found:
            errors.append(f"package {name} ({reason}) not found in {resolver.where()}")
        elif len(found) > 1:
            listed = "; ".join(c.describe() for c in found)
            errors.append(f"package {name} ({reason}) is ambiguous: {listed}")
        else:
            add(found[0], reason)

    add(_core_candidate(), "root")
    for name in uses:
        by_name(name, "uses")
    entries = []
    for path, reason in discovered:
        manifest = path / "manifest.toml"
        data = parse_manifest(manifest) if manifest.is_file() else {}
        entries.append(
            (path, reason, str(data.get("name", path.name)), set(data.get("requires", ())))
        )
    # A discovered package whose required package is nowhere yet waits: the
    # discovery that is still unresolved (an ambiguity, say) reports it.
    names = {n for _, _, n, _ in entries} | set(loaded)
    while waiting := [
        e for e in entries if any(r not in names and not resolver.candidates(r) for r in e[3])
    ]:
        entries = [e for e in entries if e not in waiting]
        names = {n for _, _, n, _ in entries} | set(loaded)
    for path, reason, name, _ in entries:
        add(Candidate(name, (path / "manifest.toml").resolve(), "path"), reason)

    while True:
        before = set(loaded)
        for pkg in list(loaded.values()):
            for req in sorted(pkg.data["requires"]):
                by_name(req, f"requires:{pkg.name}")
        if set(loaded) != before:
            continue
        accepted = {
            rel for pkg in loaded.values() for rel in (*pkg.data["accepts"], *pkg.data["user"])
        }
        pending: list[str] = []
        for rel in sorted(set(spec_relations) - accepted):
            found = [c for c in resolver.namespace_candidates(rel) if c.name not in loaded]
            origin = spec_relations[rel]
            if len(found) == 1:
                add(found[0], f"namespace:{rel}")
            elif not found:
                pending.append(
                    f"{origin}: relation {rel} is accepted by no loaded package, and no "
                    f"package in {resolver.where()} has a namespace prefixing it"
                )
            else:
                listed = "; ".join(c.describe() for c in found)
                pending.append(f"{origin}: relation {rel} matches several namespaces: {listed}")
        if set(loaded) == before:
            errors.extend(pending)
            break
    if errors:
        raise LoaderError(list(dict.fromkeys(errors)))
    return sorted(loaded.values(), key=lambda p: (p.data["rank"], p.name))


# --- lockfile ----------------------------------------------------------------


def read_lock(project: Path) -> dict[str, dict[str, str]]:
    path = project / LOCKFILE
    if not path.exists():
        return {}
    try:
        data = read_toml(path)
    except BuildlogError as exc:
        raise LoaderError([str(exc)]) from exc
    return {entry["name"]: entry for entry in data.get("package", [])}


def check_lock(project: Path, packages: list[Package]) -> None:
    """A locked package whose version, source or content changed is an error."""
    locked = read_lock(project)
    errors: list[str] = []
    for pkg in packages:
        old = locked.get(pkg.name)
        if old is None:
            continue
        new = pkg.lock_entry(project)
        for key in ("version", "source", "entry_point", "path", "hash"):
            if old.get(key) != new.get(key):
                errors.append(
                    f"{LOCKFILE}: {pkg.name} {key} changed ({old.get(key)} -> {new.get(key)}); "
                    "configure with --update-lock to accept"
                )
    if errors:
        raise LoaderError(errors)


def lock_text(project: Path, packages: list[Package]) -> str:
    lines = [
        "# Generated by `buildlog configure`: the packages this project loaded.",
        "# A changed version, source or hash is an error; accept it with --update-lock.",
        "version = 1",
    ]
    for pkg in packages:
        lines += ["", "[[package]]"]
        for key, value in pkg.lock_entry(project).items():
            lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def write_lock(project: Path, packages: list[Package]) -> None:
    text = lock_text(project, packages)
    path = project / LOCKFILE
    if path.exists() and path.read_text() == text:
        return
    # Atomically: concurrent configures of one project (parallel tests, two
    # shells) read the lock while another writes it.
    fd, tmp = tempfile.mkstemp(dir=project, prefix=f".{LOCKFILE}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
