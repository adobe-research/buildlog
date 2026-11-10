# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""buildlog-cpp: the C-family plugin for buildlog (L2-L4, clang family, probes)."""

from __future__ import annotations

from pathlib import Path


def manifest_dir() -> Path:
    """Entry point of the `buildlog.plugins` group: the plugin's manifest directory."""
    return Path(__file__).resolve().parent
