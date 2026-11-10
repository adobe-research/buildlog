#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Option 1 determinism scenarios: which LLVM does the FindHalide_LLVM probe pick,
# depending on its inputs, the environment and the state of the probe's build tree?
# Prints one line per scenario: LLVM_DIR, Clang_DIR, LLD_DIR, Halide_LLVM_VERSION.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAW="$HERE/raw/determinism"
HALIDE_CMAKE="${HALIDE_CMAKE:-$HERE/../upstream/cmake}"
ARGS="21...99;REQUIRED;COMPONENTS;WebAssembly;X86;OPTIONAL_COMPONENTS;AArch64;ARM;Hexagon;NVPTX;PowerPC;RISCV"
mkdir -p "$RAW"

probe() { # probe NAME BUILD [cmake -D args...]
    local name="$1" build="$2"
    shift 2
    cmake -S "$HERE/llvm-probe" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
        "-DPROBE_MODULE_PATH=$HALIDE_CMAKE" -DPROBE_PACKAGE=Halide_LLVM "-DPROBE_ARGS=$ARGS" \
        "-DPROBE_VAR_REGEX=^(LLVM_DIR|Clang_DIR|LLD_DIR|Halide_LLVM_VERSION)$" \
        "-DPROBE_OUT=$build/probe" "$@" >"$build.log" 2>&1 || { echo "$name: configure FAILED"; return; }
    python3 - "$name" "$build/probe/dump.json" <<'EOF'
import json, sys
v = json.load(open(sys.argv[2]))["variables"]
print(f"{sys.argv[1]:<34}", *(f"{k}={v.get(k, '-')}" for k in
      ("Halide_LLVM_VERSION", "LLVM_DIR", "Clang_DIR", "LLD_DIR")))
EOF
}

fresh() { rm -rf "$RAW/$1"; echo "$RAW/$1"; }

probe "root=llvm@22 (baseline)" "$(fresh a)" -DHalide_LLVM_ROOT=/opt/homebrew/opt/llvm@22
probe "  same tree, root=llvm@21" "$RAW/a" -DHalide_LLVM_ROOT=/opt/homebrew/opt/llvm@21
probe "root=llvm@21, fresh tree" "$(fresh b)" -DHalide_LLVM_ROOT=/opt/homebrew/opt/llvm@21
probe "no root" "$(fresh c)"
PATH="/opt/homebrew/opt/llvm/bin:$PATH" probe "no root, llvm (23) bin on PATH" "$(fresh d)"
PATH="/opt/homebrew/opt/llvm@21/bin:$PATH" probe "no root, llvm@21 bin on PATH" "$(fresh e)"
CMAKE_PREFIX_PATH=/opt/homebrew/opt/llvm@21 probe "no root, CMAKE_PREFIX_PATH=llvm@21" "$(fresh f)"
LLVM_DIR=/opt/homebrew/opt/llvm@21/lib/cmake/llvm probe "no root, env LLVM_DIR=llvm@21" "$(fresh g)"
