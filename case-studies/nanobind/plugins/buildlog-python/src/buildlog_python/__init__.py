# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""buildlog-python: Python interpreters as discovered facts, and Python
extension modules (the nanobind case study)."""

from __future__ import annotations

from pathlib import Path


def manifest_dir() -> Path:
    """Entry point of the `buildlog.plugins` group: the plugin's manifest directory."""
    return Path(__file__).resolve().parent
