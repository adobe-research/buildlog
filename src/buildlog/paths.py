# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
from pathlib import Path

UNSET_SOUFFLE = Path("<BUILDLOG_SOUFFLE unset>")  # never exists


def souffle_path() -> Path:
    value = os.environ.get("BUILDLOG_SOUFFLE")
    return Path(value) if value else UNSET_SOUFFLE


def require_souffle() -> Path:
    p = souffle_path()
    if p == UNSET_SOUFFLE:
        raise SystemExit(
            "buildlog: BUILDLOG_SOUFFLE is not set; set it to the patched souffle binary."
        )
    if not p.exists():
        raise SystemExit(
            f"buildlog: Souffle engine not found: {p}\nSet BUILDLOG_SOUFFLE to the patched souffle binary."
        )
    return p


PACKAGE_ROOT = Path(__file__).resolve().parent
CORE_RULES = PACKAGE_ROOT / "layers" / "core"
