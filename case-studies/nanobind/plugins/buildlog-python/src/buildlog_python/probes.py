# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery probes of the buildlog-python plugin.

Two questions, each answered by running the interpreter the user selected:

- python_interp: what an interpreter is (implementation, version, ABI, the
  extension-module suffixes its importer accepts, its header directory, ...),
  as `res_python_interp(req, key, value)` rows. The answers come from the
  interpreter's own `sys`, `sysconfig` and `importlib.machinery`, which is
  what scikit-build-core, FindPython and nanobind's CMake read too.
- python_package: where a distribution importable by that interpreter lives,
  its version, and whether the version satisfies a requirement (PEP 440),
  as `res_python_package(req, key, value)` rows.

The cache fingerprint is the interpreter's identity (its resolved file and
stat, and the venv's pyvenv.cfg) and, for packages, the stat of the
site-packages directories next to it, so installing or removing a package
reruns the package probe.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

INTERP_SCRIPT = r"""
import importlib.machinery, json, os, platform, sys, sysconfig
v = sys.version_info
cv = sysconfig.get_config_var
out = {
    "executable": sys.executable,
    "implementation": sys.implementation.name,
    "version": f"{v.major}.{v.minor}",
    "version_full": f"{v.major}.{v.minor}.{v.micro}",
    "version_minor": str(v.minor),
    "cache_tag": sys.implementation.cache_tag or "",
    "soabi": cv("SOABI") or "",
    "ext_suffix": cv("EXT_SUFFIX") or "",
    "include_dir": sysconfig.get_path("include"),
    "gil_disabled": "yes" if cv("Py_GIL_DISABLED") else "no",
    "sysconfig_platform": sysconfig.get_platform(),
    "machine": {"aarch64": "arm64", "AMD64": "x86_64", "amd64": "x86_64"}.get(platform.machine(), platform.machine()),
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "platlib": sysconfig.get_path("platlib"),
    "purelib": sysconfig.get_path("purelib"),
    "shared_libpython": "yes" if cv("Py_ENABLE_SHARED") or cv("PYTHONFRAMEWORK") else "no",
    "cxx": cv("CXX") or "",
}
for i, s in enumerate(importlib.machinery.EXTENSION_SUFFIXES):
    out[f"extension_suffix.{i}"] = s
out["abi3_suffix"] = next((s for s in importlib.machinery.EXTENSION_SUFFIXES if ".abi3." in s), "none")
print(json.dumps(out))
"""

PACKAGE_SCRIPT = r"""
import importlib.metadata, importlib.util, json, sys
dist, mod = sys.argv[1], sys.argv[2]
out = {}
try:
    out["version"] = importlib.metadata.version(dist)
except importlib.metadata.PackageNotFoundError:
    pass
spec = importlib.util.find_spec(mod)
if spec is not None and spec.submodule_search_locations:
    out["dir"] = list(spec.submodule_search_locations)[0]
print(json.dumps(out))
"""


def _run(argv: list[str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    argv = [os.path.expanduser(argv[0]), *argv[1:]]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
    except OSError:
        return {}
    if out.returncode != 0:
        return {}
    try:
        return {str(k): str(v) for k, v in json.loads(out.stdout).items()}
    except (ValueError, AttributeError):
        return {}


def python_interp(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    (path,) = args
    info = _run([path, "-I", "-c", INTERP_SCRIPT])
    if not info:
        return {"res_python_interp": [(req_id, "found", "no")]}
    rows = [(req_id, "found", "yes")] + [(req_id, k, v) for k, v in sorted(info.items())]
    return {"res_python_interp": rows}


def python_package(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    path, dist, module, spec = args
    info = _run([path, "-I", "-c", PACKAGE_SCRIPT, dist, module])
    rows = [(req_id, k, v) for k, v in sorted(info.items())]
    found = "version" in info and "dir" in info
    rows.append((req_id, "found", "yes" if found else "no"))
    if found:
        try:
            ok = Version(info["version"]) in SpecifierSet(spec)
        except (InvalidSpecifier, InvalidVersion):
            ok = False
        rows.append((req_id, "satisfies", "yes" if ok else "no"))
    return {"res_python_package": rows}


def _stat(p: Path) -> list:
    try:
        st = p.stat()
    except OSError:
        return [str(p), None]
    return [str(p), st.st_size, st.st_mtime_ns]


def _interp_identity(path: str) -> list:
    exe = Path(path).expanduser()
    root = exe.parent.parent
    return [str(exe), str(exe.resolve()), _stat(exe.resolve()), _stat(root / "pyvenv.cfg")]


def interp_fingerprint(args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]) -> str:
    return hashlib.sha256(json.dumps(_interp_identity(args[0])).encode()).hexdigest()


def package_fingerprint(args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]) -> str:
    root = Path(args[0]).expanduser().parent.parent
    sites = sorted((root / "lib").glob("python*/site-packages"))
    ident = [_interp_identity(args[0]), [_stat(s) for s in sites]]
    return hashlib.sha256(json.dumps(ident).encode()).hexdigest()
