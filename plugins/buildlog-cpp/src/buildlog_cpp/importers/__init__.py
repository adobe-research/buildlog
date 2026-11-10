# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""buildlog-cpp's package providers: pkg-config
(`pc`), CPS (`cps`), CMake export files (`cmake`) and buildlog's own
export as Datalog (`buildlog`), registered with
`[[provider]]` in the manifest. The core (L3_discovery.dl,
buildlog.providers) asks every provider for every wanted name under every
root, selects among the candidates and relays the rows; each provider owns
its spellings, its install layout under a prefix and its file format.

Target ids per format (each provider defines ids only in its own format's
namespace and answers only for the spellings it recognizes, so one id is
found by one format only):
- pkg-config: `pc:<module>`, found as `<dir>/<module>.pc`.
- CPS: `<Pkg>:<component>` or `<Pkg>:*` (its default components), in
  `<dir>/<Pkg>.cps`, `<dir>/<Pkg>/<Pkg>.cps` and `<dir>/<Pkg>/*/<Pkg>.cps`
  (also lower-cased), per the CPS search rules.
- CMake exports: `<Ns>::<name>` as exported, also found as `<Ns>:<name>`
  and (for `<Ns>::<Ns>`) `<Ns>`; export files under `<dir>/<Ns>*/`
  (and one subdirectory level), CMake's own `<name>*` package-directory
  rule, whose `add_library`/`add_executable` defines that exact name.
- buildlog: `<Pkg>:<component>` and `<Pkg>:*` as CPS (the same ids), in
  `<dir>/<Pkg>/<Pkg>.facts.dl`; the package is also a rule package, loaded
  with it (buildlog_dl.py).
A bare name is what the formats call a pkg-config module or a CPS package;
the providers recognize it, and the one that defines it states the binding
(`alias("libpng", "pc:libpng")`, `alias("Pkg", "Pkg:*")`, pkg-config
`Provides`); found by several, it is ambiguous until pinned (package_pin).

Package names are format-neutral: `P:comp` is component comp of package P
and `P` is the package (its default components), whichever route binds it. CPS ids are these names; a
CMake export's `P::comp` is also `P:comp`, and `P::P` is also `P` (the
convention buildlog's own CMake export and most CMake packages follow);
a source project's export_component names its targets the same way (core
L3_install.dl, package_component). So a downstream names a package's
components once, and only the user's preset chooses the route. Other
cross-format equivalences (`PNG::PNG` vs `pc:libpng`) are aliases stated
by a binding source or the project; providers never guess them.

Each target's package, by the package's own name (CPS `name`, the `.pc`
module, the `<Pkg>Config.cmake` beside a CMake export file), and its
component there are `res_pkg_package`, with the package's version
(`res_pkg_version`): what an export requiring the target names (core
L3_install.dl, "requirements on imported packages").

Under an install prefix each format searches its standard layout:
`lib/<fmt>` and `share/<fmt>` (pkgconfig, cps, cmake); a search directory
(package_search_dir) is searched as is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from buildlog.providers import load_file

from . import buildlog_dl, cmake_export, cps, pkgconfig
from .common import CONFIG_SETTINGS, MetadataError, cpu_domain, os_domain, stat_token

# Bump when importer output changes, to invalidate cached imports.
IMPORTER_VERSION = "13"


@dataclass(frozen=True)
class FormatProvider:
    name: str
    module: ModuleType
    layout: tuple[str, ...]
    namespaces: bool = False

    def dirs(self, kind: str, root: Path) -> list[Path]:
        if kind == "prefix":
            return [root / sub for sub in self.layout]
        return [root]

    def by_alias(self, name: str) -> bool:
        """Is a file found for `name` found through an alias it states, not
        an id it defines?"""
        return self.module.by_alias(name)

    def same_as(self, file: Path) -> list[Path]:
        """Files this one declares itself the same package as (verified)."""
        hook = getattr(self.module, "same_as", None)
        return hook(file) if hook is not None else []

    def find(self, name: str, kind: str, root: Path) -> list[Path]:
        return [f for d in self.dirs(kind, root) if d.is_dir() for f in self.module.find(name, d)]

    def enumerate_files(self, kind: str, root: Path, observations) -> list[Path]:
        self._observe_implementation(observations)
        return [
            file
            for directory in self.dirs(kind, root)
            for file in self.module.enumerate_files(directory, observations)
        ]

    def metadata_links(self, file: Path, observations):
        self._observe_implementation(observations)
        return self.module.metadata_links(file, observations)

    def offers(
        self, file: Path, kind: str, root: Path, names: frozenset[str], observations
    ) -> set[str]:
        return {
            name
            for directory in self.dirs(kind, root)
            for name in self.module.offers(file, directory, names, observations)
        }

    def _observe_implementation(self, observations) -> None:
        for path in observations.directory(Path(__file__).parent):
            if path.suffix == ".py":
                observations.file(path)

    def find_watched(self, name: str, kind: str, root: Path) -> list:
        return [
            [IMPORTER_VERSION, str(d), self.module.find_watched(name, d)]
            for d in self.dirs(kind, root)
        ]

    def load(self, res, results, namespace: str) -> None:
        if namespace and not self.namespaces:
            raise MetadataError(
                None, f"an import namespace applies to CMake export files, not {self.name}"
            )
        if namespace:
            self.module.import_file(res, results, namespace)
        else:
            self.module.import_file(res, results)

    def decode(self, res, vocabulary, namespace: str) -> None:
        self.load(res, vocabulary, namespace)

    def watched(self, path: Path, results, sysroot: str = "") -> list:
        return [
            IMPORTER_VERSION,
            CONFIG_SETTINGS,
            sorted(os_domain(results)),
            sorted(cpu_domain(results)),
            self.module.watched(path, sysroot) if self.name == "pc" else self.module.watched(path),
        ]


PC = FormatProvider("pc", pkgconfig, ("lib/pkgconfig", "share/pkgconfig"))
CPS = FormatProvider("cps", cps, ("lib/cps", "share/cps"))
CMAKE = FormatProvider("cmake", cmake_export, ("lib/cmake", "share/cmake"), namespaces=True)

BUILDLOG = FormatProvider("buildlog", buildlog_dl, ("share/buildlog",))

PROVIDERS = {p.name: p for p in (PC, CPS, CMAKE, BUILDLOG)}


def pkg_import(ctx, req_id: str, args: tuple[str, ...], results) -> dict[str, list]:
    """One import outside a configure (tests, tools): (format, file[, ns])."""
    fmt, file = args[:2]
    namespace = args[2] if len(args) > 2 else ""
    sysroot = args[3] if len(args) > 3 else ""
    return load_file(PROVIDERS[fmt], req_id, file, namespace, results, sysroot)


__all__ = [
    "BUILDLOG",
    "CMAKE",
    "CPS",
    "PC",
    "PROVIDERS",
    "FormatProvider",
    "pkg_import",
    "stat_token",
]
