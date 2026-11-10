# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared machinery of buildlog-cpp's package providers (pkg-config, CPS,
CMake exports). The neutral result rows, `Result` and the condition trees
are core's (buildlog.providers) and re-exported here; this module adds the
C-family pieces: package configurations, platform domains, per-format ids
and link items.
"""

from __future__ import annotations

import os
from pathlib import Path

from buildlog.providers import (  # noqa: F401  (re-exported for the providers)
    FALSE,
    RELATIONS,
    TRUE,
    Cond,
    MetadataError,
    Result,
    Vocabulary,
    and_,
    defined,
    eq,
    not_,
    or_,
    render,
    show,
    stat_token,
    under_sysroot,
)

# ---- configurations ---------------------------------------------------------

# Package configuration names (CMake's CMAKE_BUILD_TYPE, CPS `configurations`)
# to buildlog settings. Fixed and documented: a
# configuration not listed here never matches a buildlog configuration; there
# is no CMake-style fallback to "some other" configuration.
CONFIG_SETTINGS = {
    "debug": ("native.opt", "debug"),
    "release": ("native.opt", "release"),
}


# A package's own configurations that no build configuration is. CMake falls
# back to an available one when the package has none for the build's; here
# these apply to a package that has no Debug or Release block (the importers).
PACKAGE_ONLY_CONFIGS = ("relwithdebinfo", "minsizerel")


def config_cond(name: str) -> Cond:
    """The condition under which package configuration `name` applies."""
    setting = CONFIG_SETTINGS.get(name.lower())
    return eq(*setting) if setting else FALSE


def package_config_cond(name: str) -> Cond:
    """The condition under which a package's own configuration `name` (a
    CMake IMPORTED_CONFIGURATIONS block, a CPS configuration) is the one used:
    core's `native.package_config` attribute, native.opt unless the invoker
    maps it (package_config_map, L3_native.dl; CMake's
    MAP_IMPORTED_CONFIG_<CONFIG>, stated, never a fallback)."""
    setting = CONFIG_SETTINGS.get(name.lower())
    return eq("native.package_config", setting[1]) if setting else FALSE


def is_mapped_config(name: str) -> bool:
    return name.lower() in CONFIG_SETTINGS


# ---- platforms --------------------------------------------------------------


def os_domain(results: dict[str, set[tuple[str, ...]]] | Vocabulary) -> set[str]:
    """platform.os values of the inventory (conditions outside it are false)."""
    if isinstance(results, Vocabulary):
        return set(results.os)
    return {v for _req, _p, k, v in results.get("res_platform_constraint", set()) if k == "os"}


def cpu_domain(results: dict[str, set[tuple[str, ...]]] | Vocabulary) -> set[str]:
    if isinstance(results, Vocabulary):
        return set(results.cpu)
    return {v for _req, _p, k, v in results.get("res_platform_constraint", set()) if k == "cpu"}


def os_cond(os_name: str, domain: set[str]) -> Cond:
    return eq("platform.os", os_name) if os_name in domain else FALSE


# ---- results ----------------------------------------------------------------

# Target ids per format: every importer defines ids
# only in its own format's namespace and never states a cross-format
# equivalence it does not read in the metadata. pkg-config modules are
# `pc:<module>`, CPS components `<Pkg>:<component>` (the CPS spelling) and
# a package's default components `<Pkg>:*`, CMake targets their exported
# names as-is (`<Ns>::<name>`). A CPS package may not be named `pc`.
PC_PREFIX = "pc:"
CPS_DEFAULT = "*"


def pc_id(module: str) -> str:
    return PC_PREFIX + module


def cps_id(pkg: str, component: str) -> str:
    return f"{pkg}:{component}"


# ---- link items -------------------------------------------------------------

LIB_SUFFIXES = (".tbd", ".dylib", ".so", ".a")


def resolve_lib(name: str, dirs: list[str], observations=None) -> str | None:
    """`-l<name>` against `-L` directories, in order, as ld64 searches them."""
    for d in dirs:
        for suffix in LIB_SUFFIXES:
            p = Path(d) / f"lib{name}{suffix}"
            if observations.is_file(p) if observations is not None else p.is_file():
                return os.path.normpath(str(p))
    return None


def is_library_path(item: str) -> bool:
    base = os.path.basename(item)
    return os.path.isabs(item) and (base.endswith(LIB_SUFFIXES) or ".so." in base)


def exact(p: Path) -> bool:
    """p exists with exactly this spelling (case-insensitive file systems)."""
    try:
        return p.name in os.listdir(p.parent)
    except OSError:
        return False
