# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Running Soufflé over a configured build without re-running discovery.

`Model` is a configured build directory: the composed program, the input
facts of the final discovery round (spec, package and probe-result facts),
the derived relations, and the source map. Provenance sessions evaluate the
same program over the same inputs with `-t explain`, so no probe runs and
nothing is recomposed.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import BuildlogError
from ..facts import read_rows
from ..paths import require_souffle
from .program import ProgramIndex


@dataclass
class Model:
    builddir: Path
    program: Path
    facts_in: Path
    derived: Path
    index: ProgramIndex
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def source_map_path(self) -> Path:
        return self.facts_in / "source_map.json"

    def source_map(self) -> dict[str, list]:
        path = self.source_map_path
        return json.loads(path.read_text()) if path.exists() else {}

    def derived_rows(self, rel: str) -> list[tuple[str, ...]]:
        return read_rows(self.derived / f"{rel}.csv")

    @property
    def workdir(self) -> Path:
        d = self.builddir / "why"
        d.mkdir(parents=True, exist_ok=True)
        return d


def load_model(builddir: Path) -> Model:
    builddir = builddir.resolve()
    program = builddir / "program" / "build.dl"
    derived = builddir / "souffle" / "facts"
    if not program.exists() or not derived.exists():
        raise BuildlogError(f"{builddir} is not a configured build directory")
    metrics_path = builddir / "metrics.json"
    final_round = 0
    if metrics_path.exists():
        final_round = json.loads(metrics_path.read_text()).get("final_round", 0)
    facts_in = builddir / "facts-in" / f"round-{final_round}"
    return Model(builddir, program, facts_in, derived, ProgramIndex(builddir))


def _souffle_cmd(model: Model, out: Path, provenance: bool = True) -> list[str]:
    cmd = [str(require_souffle()), "-w", "-F", str(model.facts_in), "-D", str(out)]
    if provenance:
        cmd += ["-t", "explain"]
    return cmd


def explain(model: Model, atoms: list[str], depth: int = 100000) -> list[dict]:
    """One provenance session: `explain` each atom, full depth, JSON format.

    Each proof is written to its own file with the REPL's `output` command, so
    diagnostics on stdout cannot interleave with proofs."""
    work = model.workdir / "explain"
    if work.exists():
        for f in work.iterdir():
            f.unlink()
    work.mkdir(parents=True, exist_ok=True)
    lines = ["format json", f"setdepth {depth}"]
    for i, atom in enumerate(atoms):
        lines += [f"output {work / f'proof-{i}.json'}", f"explain {atom}"]
    lines.append("exit")
    start = time.perf_counter()
    cp = subprocess.run(
        [*_souffle_cmd(model, work), str(model.program)],
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    model.timings["souffle_explain_s"] = time.perf_counter() - start
    out = []
    for i, atom in enumerate(atoms):
        path = work / f"proof-{i}.json"
        text = path.read_text() if path.exists() else ""
        if not text.strip():
            detail = (cp.stdout + cp.stderr).strip().splitlines()
            raise BuildlogError(
                f"souffle explain {atom}: no proof" + (f" ({detail[-1]})" if detail else "")
            )
        raw = json.loads(text)
        raw["_bytes"] = len(text.encode())
        out.append(raw)
    if cp.returncode:
        raise BuildlogError(f"souffle exited with {cp.returncode}: {cp.stderr.strip()}")
    return out


class Repl:
    """An interactive provenance session on a pseudo-terminal.

    Soufflé only prints REPL prompts and informational messages (the rule
    list of `explainnegation`, its variable prompts, "The tuple exists")
    when stdin is a terminal, and `explainnegation` needs answers to those
    prompts. A pty lets the driver read each prompt and answer it."""

    PROMPT = "Enter command > "

    def __init__(self, model: Model):
        import pty

        self.model = model
        work = model.workdir / "repl"
        work.mkdir(parents=True, exist_ok=True)
        master, slave = pty.openpty()
        import termios

        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        self.proc = subprocess.Popen(
            [*_souffle_cmd(model, work), str(model.program)],
            stdin=slave,
            stdout=slave,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        os.close(slave)
        self.fd = master
        self.buffer = ""
        self.read_until([self.PROMPT], timeout=600)

    def read_until(self, markers: list[str], timeout: float = 60) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while True:
            for marker in markers:
                idx = self.buffer.find(marker)
                if idx >= 0:
                    text = self.buffer[:idx]
                    self.buffer = self.buffer[idx + len(marker) :]
                    return text.replace("\r\n", "\n"), marker
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BuildlogError(f"souffle REPL timed out waiting for {markers!r}")
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                text, self.buffer = self.buffer, ""
                return text.replace("\r\n", "\n"), ""
            self.buffer += chunk.decode(errors="replace")

    def send(self, line: str) -> None:
        os.write(self.fd, (line + "\n").encode())

    def command(self, line: str) -> str:
        self.send(line)
        text, _ = self.read_until([self.PROMPT])
        return text

    def close(self) -> None:
        try:
            self.send("exit")
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
        finally:
            os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def run_program(model: Model, program_text: str, name: str) -> Path:
    """Evaluate an ad-hoc program (e.g. a query) over the model's inputs."""
    work = model.workdir / name
    work.mkdir(parents=True, exist_ok=True)
    for f in [*work.glob("*.csv"), *work.glob("*.json")]:
        f.unlink()
    path = work / f"{name}.dl"
    path.write_text(program_text)
    start = time.perf_counter()
    cp = subprocess.run(
        [*_souffle_cmd(model, work, provenance=False), str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    model.timings[f"souffle_{name}_s"] = time.perf_counter() - start
    if cp.returncode:
        message = (cp.stdout + cp.stderr).strip()
        raise BuildlogError(f"query failed:\n{message}")
    return work
