# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Probe entry points of the test plugins."""

from __future__ import annotations

from pathlib import Path


def runaway(ctx, req_id, args, results):
    return {"res_runaway": [(req_id, args[0] + "x")]}


def discover(ctx, req_id, args, results):
    """Read a rule-package path (relative to the file) from a fixture file."""
    (file,) = args
    path = Path(file)
    found = (path.parent / path.read_text().strip()).resolve()
    return {"res_test_discover": [(req_id, str(found))]}


def discover_fingerprint(args, results):
    """The pointer file's content: all `discover` reads."""
    try:
        return Path(args[0]).read_text()
    except OSError:
        return ""
