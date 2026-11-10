# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build Halide's HelloWasm app with buildlog, run it under Node and check its frames.

    set -a; . env.local.sh; set +a   # BUILDLOG_HALIDE_PREFIX, EMSDK, EM_BINARYEN_ROOT
    uv run --frozen python scripts/hellowasm_check.py [--build DIR] [--frames N] [--json OUT]

One configure of case-studies/halide/apps/hellowasm builds the host generator, runs it for
the four wasm32-emscripten configurations and links each configuration's index.js/index.wasm.
Each of the four is run headless under Node (scripts/hellowasm_node.cjs) for N frames of the
app's own main loop. The reference is the same three pipelines built by the same graph for the
build machine (the libraries the host `run` program links), driven by
scripts/hellowasm_reference.cpp exactly as core.cpp drives them. The script reports, per
configuration, the SHA-256 of the last frame, the pixels that differ from the reference and the
largest difference in one 8-bit channel. It exits 1 unless every configuration runs N frames,
the four frames are identical, and no channel differs from the reference by more than one unit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "case-studies" / "halide" / "apps" / "hellowasm"
HOST = "arm64-macos.release.none"
WASM = "wasm32-emscripten.release.none"
VARIANTS = ["", ".simd128", ".threads", ".simd128.threads"]
LIBS = ["reaction_diffusion_init", "reaction_diffusion_update", "reaction_diffusion_render"]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", type=Path, default=ROOT / "build" / "hellowasm-check")
    ap.add_argument("--frames", type=int, default=64)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    prefix = os.environ["BUILDLOG_HALIDE_PREFIX"]
    node = shutil.which("node")
    if node is None:
        sys.exit("node not found")
    build = args.build.resolve()
    run(
        [
            sys.executable, "-m", "buildlog", "configure", str(APP), "-C", str(build),
            "--package-path", str(ROOT / "case-studies" / "halide" / "packages"),
            "-D", f"HALIDE_PREFIX={prefix}",
            "--user", str(APP / "presets" / "homebrew-arm64.toml"),
        ],
        stdout=subprocess.DEVNULL,
    )  # fmt: skip
    souffle = build / "souffle"
    run(["ninja", "-C", str(souffle), "-j8"], stdout=subprocess.DEVNULL)

    lib, gen = souffle / "lib" / HOST, souffle / "gen" / HOST
    ref_bin = build / "hellowasm-reference"
    run(
        [
            "clang++", "-std=c++17", "-O2", f"-I{prefix}/include",
            *[f"-I{gen / name}" for name in LIBS],
            str(ROOT / "scripts" / "hellowasm_reference.cpp"),
            str(lib / "libreaction_diffusion_init.a"),
            str(lib / "libreaction_diffusion_init.runtime.a"),
            *[str(lib / f"lib{name}.a") for name in LIBS[1:]],
            "-o", str(ref_bin),
        ]
    )  # fmt: skip
    ref_file = build / "frame-host.bin"
    run([str(ref_bin), str(args.frames), str(ref_file)])
    ref = ref_file.read_bytes()

    result: dict = {
        "frames": args.frames,
        "pixels": len(ref) // 4,
        "host_sha256": hashlib.sha256(ref).hexdigest(),
        "variants": {},
    }
    ok = True
    shas = set()
    for v in VARIANTS:
        dump = build / f"frame-wasm{v or '.base'}.bin"
        js = souffle / "bin" / f"{WASM}{v}" / "index.js"
        p = subprocess.run(
            [node, str(ROOT / "scripts" / "hellowasm_node.cjs"), str(js), str(args.frames)],
            env={**os.environ, "HELLOWASM_DUMP": str(dump)},
            capture_output=True,
            text=True,
            check=False,
        )
        out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
        row = {"exit": p.returncode, **out}
        if p.returncode == 0 and dump.exists():
            got = dump.read_bytes()
            px = [i for i in range(0, len(ref), 4) if got[i : i + 4] != ref[i : i + 4]]
            row["pixels_differing"] = len(px)
            row["max_channel_diff"] = max(
                (abs(got[i] - ref[i]) for i in range(len(ref)) if got[i] != ref[i]), default=0
            )
            shas.add(row["sha256"])
        ok &= p.returncode == 0 and row.get("frames") == args.frames
        ok &= row.get("max_channel_diff", 99) <= 1
        result["variants"][v.lstrip(".") or "base"] = row
    result["variants_identical"] = len(shas) == 1
    ok &= result["variants_identical"]
    result["ok"] = ok
    text = json.dumps(result, indent=1, sort_keys=True)
    print(text)
    if args.json:
        args.json.write_text(text + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
