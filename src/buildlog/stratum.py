# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build-time strata: `buildlog -E collate PACKAGE:STRATUM ARGS...`.

A plugin declares a stratum in its manifest (`[[stratum]]`: a Datalog program
and a Python driver). The build action that runs it reads what the build
produced (C++ scan results) and writes build-graph files (Ninja dyndep). The
configure program never reads a stratum's relations (layers._check_strata),
so nothing a build discovers changes the configure model.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path

from .layers import parse_manifest
from .packages import ENTRY_POINT_GROUP
from .paths import require_souffle


class StratumError(Exception):
    pass


def find_stratum(package: str, name: str):
    eps = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == package]
    if len(eps) != 1:
        raise StratumError(f"no unique plugin named {package!r}")
    manifest = eps[0].load()()
    data = parse_manifest(manifest / "manifest.toml" if manifest.is_dir() else manifest)
    for st in data["stratum"]:
        if st.name == name:
            return st
    raise StratumError(f"{package} declares no stratum {name!r}")


def collate(spec: str, *args: str) -> None:
    """Run a stratum's driver on the command line's arguments. The driver
    gets the stratum's program, the Soufflé binary and the arguments; it
    returns an error message for a build error (exit 1, no outputs)."""
    package, _, name = spec.partition(":")
    st = find_stratum(package, name)
    module, _, func = st.entry.partition(":")
    driver = getattr(importlib.import_module(module), func)
    message = driver(st.program, require_souffle(), list(args))
    if message:
        raise StratumError(message)


def dynamic_edges(builddir: Path, output: str) -> list[str]:
    """The dynamic dyndep edges a build-time stratum recorded for `output`
    (its `*.prov` files under the build tree, JSON with an `edges` list of
    output, input, because), as lines for `why`."""
    lines: list[str] = []
    for prov in sorted((builddir / "souffle").rglob("*.prov")):
        try:
            edges = json.loads(prov.read_text()).get("edges", [])
        except (OSError, ValueError):
            continue
        lines += [f"  {e['input']}: {e['because']}" for e in edges if e["output"] == output]
    return lines
