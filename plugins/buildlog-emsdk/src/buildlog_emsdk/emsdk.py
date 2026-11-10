# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery of Emscripten, found the way emsdk's own tooling finds it: EMSDK
names the install (upstream/emscripten holds em++, emcc, emar). Not set: no
platform or toolchain.

Soufflé requests the probe (layers/L2_emsdk.dl); the rows are what it found,
under the res_cross_emsdk_* names that buildlog-cpp's capability probes read
beside the host inventory's.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from buildlog_cpp.probes import file_identity, host_platform

PLATFORM = ("wasm32-emscripten", "wasm32-unknown-emscripten")


def emscripten_dir(root: Path) -> Path | None:
    d = root / "upstream" / "emscripten"
    return d if (d / "em++").exists() else None


def emscripten_version(d: Path) -> str:
    try:
        return (d / "emscripten-version.txt").read_text().strip().strip('"')
    except OSError:
        return "unknown"


def inventory(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    rows: dict[str, list[tuple[str, ...]]] = {
        "res_cross_emsdk_status": [],
        "res_cross_emsdk_platform": [],
        "res_cross_emsdk_platform_constraint": [],
        "res_cross_emsdk_toolchain": [],
        "res_cross_emsdk_toolchain_tool": [],
        "res_cross_emsdk_toolchain_runs_on": [],
        "res_cross_emsdk_toolchain_target": [],
    }
    emsdk = os.environ.get("EMSDK")
    em = emscripten_dir(Path(emsdk)) if emsdk else None
    if em:
        p, triple = PLATFORM
        rows["res_cross_emsdk_platform"].append((req_id, p, triple))
        rows["res_cross_emsdk_platform_constraint"] += [
            (req_id, p, "cpu", "wasm32"),
            (req_id, p, "os", "emscripten"),
        ]
        rows["res_cross_emsdk_toolchain_target"].append((req_id, "emscripten", p))
        rows["res_cross_emsdk_toolchain"].append(
            (req_id, "emscripten", "emscripten", emscripten_version(em))
        )
        rows["res_cross_emsdk_toolchain_tool"] += [
            (req_id, "emscripten", "cxx", str(em / "em++")),
            (req_id, "emscripten", "cc", str(em / "emcc")),
            (req_id, "emscripten", "ar", str(em / "emar")),
        ]
        rows["res_cross_emsdk_toolchain_runs_on"].append((req_id, "emscripten", host_platform()))
    rows["res_cross_emsdk_status"].append((req_id, "emscripten" if em else "none"))
    return rows


def inventory_fingerprint(args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]) -> str:
    """Cache fingerprint: EMSDK and the identity of the tools and version file
    the probe reads."""
    emsdk = os.environ.get("EMSDK")
    files: dict[str, list] = {}
    if emsdk:
        d = f"{emsdk}/upstream/emscripten"
        for name in ("em++", "emcc", "emar", "emscripten-version.txt"):
            files[f"{d}/{name}"] = file_identity(f"{d}/{name}")
    ident = {"platform": sys.platform, "env": {"EMSDK": emsdk}, "files": files}
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()
