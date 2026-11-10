# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery of the CUDA toolkit and of what it accepts.

Two probes, requested from layers/L2_cuda.dl:

* `toolkit` finds one toolkit the way CMake's FindCUDAToolkit and nvcc's users
  do: CUDAToolkit_ROOT, CUDA_PATH, nvcc on PATH, /usr/local/cuda, first hit.
  Rows: version, root, target directory and the platform it serves, the
  architectures nvcc lists, its tools and its libraries.
* `host_check` asks the toolkit whether it takes a host compiler: nvcc
  preprocesses an empty .cu with -ccbin <host>, and host_config.h, the
  toolkit's own statement of the relation, either accepts or says why not.
  The answer and the toolkit's message are the facts.

Nothing here decides: the rows are what was found and what nvcc said.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from buildlog_cpp.probes import file_identity, host_platform

# A toolkit's targets/<name> directory and the platform it serves. A name
# that already is a platform (a test fixture's) serves itself.
TARGET_PLATFORMS = {
    "sbsa-linux": "arm64-linux",
    "aarch64-linux": "arm64-linux",
    "x86_64-linux": "x86_64-linux",
}

# Libraries a project links: name -> (shared file, static file). cudadevrt
# is static only.
LIBRARIES = {
    "cudart": ("libcudart.so", "libcudart_static.a"),
    "cudadevrt": (None, "libcudadevrt.a"),
    "cublas": ("libcublas.so", "libcublas_static.a"),
    "cublasLt": ("libcublasLt.so", "libcublasLt_static.a"),
    "curand": ("libcurand.so", "libcurand_static.a"),
    "cufft": ("libcufft.so", "libcufft_static.a"),
    "cusparse": ("libcusparse.so", "libcusparse_static.a"),
    "cusolver": ("libcusolver.so", "libcusolver_static.a"),
}

ENV_VARS = ("CUDAToolkit_ROOT", "CUDA_PATH", "PATH")


def candidate_roots() -> list[Path]:
    out: list[Path] = []
    for var in ("CUDAToolkit_ROOT", "CUDA_PATH"):
        if os.environ.get(var):
            out.append(Path(os.environ[var]))
    nvcc = shutil.which("nvcc")
    if nvcc:
        out.append(Path(os.path.realpath(nvcc)).parent.parent)
    out.append(Path("/usr/local/cuda"))
    return out


def find_toolkit() -> Path | None:
    for root in candidate_roots():
        if (root / "bin" / "nvcc").exists():
            return root
    return None


def target_dir(root: Path) -> tuple[str, str] | None:
    """The target directory serving this machine's platform, with that platform."""
    host = host_platform()
    names = sorted(d.name for d in (root / "targets").glob("*") if d.is_dir())
    for n in names:
        if TARGET_PLATFORMS.get(n, n) == host:
            return n, host
    return None


def run(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as e:
        return 127, str(e)
    return r.returncode, r.stdout + r.stderr


def nvcc_version(nvcc: str) -> str:
    _, out = run([nvcc, "--version"])
    m = re.search(r"release (\d+\.\d+)", out)
    return m.group(1) if m else "unknown"


def listed(nvcc: str, option: str) -> list[str]:
    rc, out = run([nvcc, option])
    return [w for w in out.split() if re.fullmatch(r"(sm|compute)_\d+[af]?", w)] if rc == 0 else []


def toolkit(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    rows: dict[str, list[tuple[str, ...]]] = {
        "res_cuda_toolkit": [],
        "res_cuda_toolkit_code": [],
        "res_cuda_toolkit_tool": [],
        "res_cuda_toolkit_lib": [],
        "res_cuda_toolkit_status": [],
    }
    root = find_toolkit()
    if root is None:
        rows["res_cuda_toolkit_status"].append((req_id, "not found"))
        return rows
    td = target_dir(root)
    if td is None:
        have = ", ".join(sorted(d.name for d in (root / "targets").glob("*"))) or "none"
        rows["res_cuda_toolkit_status"].append(
            (req_id, f"{root} has no targets/ directory for {host_platform()} (has: {have})")
        )
        return rows
    tdir, platform = td
    base = root / "targets" / tdir
    nvcc = str(root / "bin" / "nvcc")
    include = base / "include" if (base / "include").is_dir() else root / "include"
    rows["res_cuda_toolkit"].append((req_id, nvcc_version(nvcc), str(root), platform, str(include)))
    for opt in ("--list-gpu-code", "--list-gpu-arch"):
        rows["res_cuda_toolkit_code"] += [(req_id, c) for c in listed(nvcc, opt)]
    for role, rel in (("nvcc", "bin/nvcc"), ("ptxas", "bin/ptxas"), ("nvlink", "bin/nvlink")):
        if (root / rel).exists():
            rows["res_cuda_toolkit_tool"].append((req_id, role, str(root / rel)))
    # Thrust and CUB ship in the toolkit's cccl directory (CUDA 12.x on)
    if (Path(include) / "cccl").is_dir():
        rows["res_cuda_toolkit_lib"].append(
            (req_id, "thrust", "headers", str(Path(include) / "cccl"))
        )
    libdirs = [base / "lib", root / "lib64"]
    for name, (shared, static) in LIBRARIES.items():
        for kind, fname in (("shared", shared), ("static", static)):
            if not fname:
                continue
            for d in libdirs:
                if (d / fname).exists():
                    rows["res_cuda_toolkit_lib"].append((req_id, name, kind, str(d / fname)))
                    break
    rows["res_cuda_toolkit_status"].append((req_id, "found"))
    return rows


def toolkit_fingerprint(args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]) -> str:
    root = find_toolkit()
    files: dict[str, list] = {}
    if root is not None:
        files[str(root / "bin" / "nvcc")] = file_identity(str(root / "bin" / "nvcc"))
        for d in sorted((root / "targets").glob("*")):
            files[str(d)] = file_identity(str(d))
    ident = {
        "platform": sys.platform,
        "env": {v: os.environ.get(v) for v in ENV_VARS},
        "files": files,
    }
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()


def refusal(output: str) -> str:
    """The toolkit's own words: its error lines, without the paths' noise."""
    lines = [ln.strip() for ln in output.splitlines() if "error" in ln or "fatal" in ln]
    text = " | ".join(lines or [ln.strip() for ln in output.splitlines() if ln.strip()])
    return re.sub(r"\s+", " ", text)[:600]


def host_check(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    nvcc, tc, cxx = args
    d = ctx.workdir / hashlib.sha256(req_id.encode()).hexdigest()[:16]
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    src = d / "empty.cu"
    src.write_text("")
    rc, out = run([nvcc, "-ccbin", cxx, "-x", "cu", "-E", str(src), "-o", str(d / "empty.i")])
    shutil.rmtree(d, ignore_errors=True)
    status, message = ("ok", "") if rc == 0 else ("refused", refusal(out))
    return {"res_cuda_host_check": [(req_id, tc, status, message)]}


def host_check_fingerprint(args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]) -> str:
    nvcc, _tc, cxx = args
    ident = {
        "args": list(args),
        "files": {nvcc: file_identity(nvcc), cxx: file_identity(cxx)},
    }
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()
