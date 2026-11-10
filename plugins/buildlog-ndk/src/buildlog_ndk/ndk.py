# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery of the Android NDK, found the way the NDK's own tooling finds
it: ANDROID_NDK_ROOT names the install. Not set: no platforms or toolchain.

Soufflé requests the probe (layers/L2_ndk.dl); the rows are what it found,
under the res_cross_ndk_* names that buildlog-cpp's capability probes read
beside the host inventory's. Android ABIs without a platform here (armeabi-v7a,
x86) are not offered: nothing built so far targets them.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from buildlog_cpp.probes import file_identity, host_platform

# Android ABI -> (platform name, clang triple without the API level, cpu).
# The NDK's other ABIs (armeabi-v7a, x86) have no platform here: nothing
# built so far targets them.
ANDROID_ABIS = [
    ("arm64-android", "aarch64-linux-android", "arm64"),
    ("x86_64-android", "x86_64-linux-android", "x86_64"),
]


def ndk_bin(root: Path) -> Path | None:
    """The NDK's LLVM bin directory for the build machine (one prebuilt host
    directory: darwin-x86_64, linux-x86_64)."""
    prebuilt = root / "toolchains" / "llvm" / "prebuilt"
    for d in sorted(prebuilt.glob("*")):
        if (d / "bin" / "clang++").exists():
            return d / "bin"
    return None


def ndk_version(root: Path) -> str:
    try:
        for line in (root / "source.properties").read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "Pkg.Revision":
                return v.strip()
    except OSError:
        pass
    return "unknown"


def ndk_min_api(root: Path) -> int:
    try:
        return int(json.loads((root / "meta" / "platforms.json").read_text())["min"])
    except (OSError, ValueError, KeyError):
        return 21


def inventory(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    rows: dict[str, list[tuple[str, ...]]] = {
        "res_cross_ndk_status": [],
        "res_cross_ndk_platform": [],
        "res_cross_ndk_platform_constraint": [],
        "res_cross_ndk_toolchain": [],
        "res_cross_ndk_toolchain_tool": [],
        "res_cross_ndk_toolchain_runs_on": [],
        "res_cross_ndk_toolchain_target": [],
        "res_cross_ndk_toolchain_arg": [],
        "res_cross_ndk_toolchain_probe_triple": [],
    }
    host = host_platform()
    ndk = os.environ.get("ANDROID_NDK_ROOT")
    bin_dir = ndk_bin(Path(ndk)) if ndk else None
    if ndk and bin_dir:
        for p, triple, cpu in ANDROID_ABIS:
            rows["res_cross_ndk_platform"].append((req_id, p, triple))
            rows["res_cross_ndk_platform_constraint"] += [
                (req_id, p, "cpu", cpu),
                (req_id, p, "os", "android"),
            ]
            rows["res_cross_ndk_toolchain_target"].append((req_id, "ndk", p))
            # The NDK links only for a triple with an API level; the project
            # chooses its own, so probes use the NDK's oldest.
            rows["res_cross_ndk_toolchain_probe_triple"].append(
                (req_id, "ndk", p, f"{triple}{ndk_min_api(Path(ndk))}")
            )
            rows["res_cross_ndk_toolchain_arg"].append(
                (req_id, "ndk", p, "--sysroot=", str(bin_dir.parent / "sysroot"))
            )
        rows["res_cross_ndk_toolchain"].append((req_id, "ndk", "clang", ndk_version(Path(ndk))))
        rows["res_cross_ndk_toolchain_tool"] += [
            (req_id, "ndk", "cxx", str(bin_dir / "clang++")),
            (req_id, "ndk", "cc", str(bin_dir / "clang")),
            (req_id, "ndk", "ar", str(bin_dir / "llvm-ar")),
        ]
        rows["res_cross_ndk_toolchain_runs_on"].append((req_id, "ndk", host))
    found = ",".join(t for _r, t, *_ in rows["res_cross_ndk_toolchain"]) or "none"
    rows["res_cross_ndk_status"].append((req_id, found))
    return rows


def inventory_fingerprint(args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]) -> str:
    """Cache fingerprint: the SDK variables and the identity of the tools and
    version files the probe reads."""
    files: dict[str, list] = {}
    ndk = os.environ.get("ANDROID_NDK_ROOT")
    if ndk:
        files[ndk + "/source.properties"] = file_identity(ndk + "/source.properties")
        b = ndk_bin(Path(ndk))
        if b:
            for name in ("clang++", "clang", "llvm-ar"):
                files[str(b / name)] = file_identity(str(b / name))
    ident = {
        "platform": sys.platform,
        "env": {"ANDROID_NDK_ROOT": ndk},
        "files": files,
    }
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()
