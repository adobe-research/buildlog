# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Plugin fixtures for the buildlog tests: each entry point is a manifest directory."""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent


def runaway() -> Path:
    return HERE / "runaway"


def bad_probe() -> Path:
    return HERE / "bad_probe"


def bad_entry() -> Path:
    return HERE / "bad_entry"


def discover() -> Path:
    return HERE / "discover"


def version_skew() -> Path:
    return HERE / "version_skew"


def provider() -> Path:
    return HERE / "provider"
