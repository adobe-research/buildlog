# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Determinism across processes of what seeding compares (discovery._seed)."""

from __future__ import annotations

import os
import subprocess
import sys

# Cache keys and the replay's static digest are compared across processes, so
# nothing they hash may follow Python's per-process string hash order.
_HASH_ORDER_PROBE = r"""
import json, sys
from pathlib import Path
from buildlog.facts import ProgramSchema, desugar_conditions, tuple_key
from buildlog_cpp.importers import cps

schema = ProgramSchema(
    fields={"prop_when": ["t", "p", "v", "s", "cond"]},
    types={"prop_when": ["symbol"] * 5},
)
rows = {"prop_when": {("lib", "cc.define", f"D{i}", "private", "!A & !B") for i in range(8)}}
source = {tuple_key("prop_when", r): [f"spec.toml:{i}"] for i, r in enumerate(sorted(rows["prop_when"]))}
desugar_conditions(rows, source, schema)
cond = [k for k in source if json.loads(k)[0] == "condition"]
print(json.dumps([source[cond[0]], cps.find_watched("Python3:Interpreter", Path(sys.argv[1]))]))
"""


def test_cache_keys_and_origins_do_not_follow_hash_order(tmp_path):
    outs = {
        subprocess.run(
            [sys.executable, "-c", _HASH_ORDER_PROBE, str(tmp_path)],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in range(12)
    }
    assert len(outs) == 1
