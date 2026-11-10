# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The order of an unconstrained segment (include dirs, link items) and of a
build line's inputs must not depend on where the checkout lives: paths under
the workspace sort after the system and package-manager prefixes, whatever the
checkout's own path (a checkout under /Users used to sort before /opt)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from buildlog import emit

# Names after every checkout path a test runs in ("/private/var/...", "/tmp/...").
EXTERNAL = ["/zzz-prefix/Cellar/openssl@3/include", "/zzz-prefix/include"]
INSIDE = ["llvm-install/lld/include", "llvm-install/llvm/include", "upstream/spirv/include"]


def configure(tmp_path: Path, checkout: str) -> str:
    ws = tmp_path / checkout
    (ws / ".git").mkdir(parents=True)
    project = ws / "proj"
    project.mkdir()
    facts, build = tmp_path / f"facts-{checkout}", ws / "build"
    facts.mkdir()
    build.mkdir()
    dirs = EXTERNAL + [f"{ws}/{d}" for d in INSIDE]
    rows = {
        "action": ["out:o.o\tCC"],
        "action_out": ["out:o.o\tout:o.o"],
        "action_arg": ["out:o.o\t0\t\t/usr/bin/cc"],
        "action_seq": [f"out:o.o\t35\t{d}" for d in dirs],
        "action_seq_spelled": [f"out:o.o\t35\t{d}\t-isystem \t{d}" for d in dirs],
        "action_in": [
            f"out:o.o\t{ws}/llvm-install/lld/lib/liba.a\texplicit",
            "out:o.o\t/zzz-prefix/lib/libb.a\texplicit",
        ],
    }
    for name, lines in rows.items():
        (facts / f"{name}.csv").write_text("".join(line + "\n" for line in lines))
    emit.main(str(facts), str(project), str(build))
    text = (build / "build.ninja").read_text().replace(str(ws), "<WS>")
    return re.sub(r"\.\./[./]*", "<REL>/", text)


@pytest.mark.parametrize("name", ["aaa", "zzz"])
def test_system_prefixes_sort_before_the_workspace(tmp_path, name):
    text = configure(tmp_path, name)
    cmd = next(line for line in text.splitlines() if line.startswith("  cmd ="))
    dirs = re.findall(r"-isystem (\S+)", cmd)
    assert dirs == EXTERNAL + [f"<WS>/{d}" for d in INSIDE]
    build = next(line for line in text.splitlines() if line.startswith("build "))
    assert build.index("/zzz-prefix/lib/libb.a") < build.index("llvm-install/lld/lib/liba.a")


def test_the_graph_is_the_same_wherever_the_checkout_is(tmp_path):
    assert configure(tmp_path, "aaa") == configure(tmp_path, "zzz")
