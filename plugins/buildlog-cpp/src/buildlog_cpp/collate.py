# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Driver of the C++ modules collator (strata/modules.dl).

`buildlog -E collate buildlog-cpp:modules` arguments (paths are relative to
the build directory, as Ninja runs the action there):

  --family=clang|gcc
  --dd=FILE --prov=FILE              the dyndep file and provenance to write
  --tu=KIND,OWNER,SRC,DDI,OUT,MAP    a compile to collate (KIND obj | bmi)
  --iface=PROVIDER,SRC,VIS,DDI,BMI   a declared interface unit
  --sees=U,P                         target U sees P's public interfaces

The scan results are P1689 files (clang-scan-deps, GCC -fdeps-format=p1689r5).
Outputs are written only when their content changes (the action is restat).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _read_ddi(path: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]], list[str]]:
    """(provides, requires, errors) of one scan result."""
    provides, requires, errors = [], [], []
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        return [], [], [f"cannot read scan result {path}: {exc}"]
    for rule in data.get("rules", []):
        for p in rule.get("provides", []):
            provides.append((p["logical-name"], "true" if p.get("is-interface", True) else "false"))
        for r in rule.get("requires", []):
            if r.get("lookup-method", "by-name") != "by-name":
                errors.append(f"{path}: header units are not supported ({r['logical-name']})")
            requires.append((r["logical-name"],))
    return provides, requires, errors  # type: ignore[return-value]


def _dd_text(tus: list[dict], needs: dict[str, list[str]], family: str) -> str:
    def esc(p: str) -> str:
        return p.replace("$", "$$").replace(" ", "$ ").replace(":", "$:")

    lines = ["ninja_dyndep_version = 1"]
    for tu in sorted(tus, key=lambda t: t["out"]):
        ins = " ".join(esc(b) for b in sorted(needs.get(tu["out"], [])))
        lines.append(f"build {esc(tu['out'])}: dyndep" + (f" | {ins}" if ins else ""))
    return "\n".join(lines) + "\n"


def _modmap_text(rows: list[tuple[str, str]], family: str) -> str:
    if family == "gcc":
        return "".join(f"{n} {b}\n" for n, b in sorted(rows))
    return "".join(f"-fmodule-file={n}={b}\n" for n, b in sorted(rows))


def _write_if_changed(path: str, text: str) -> None:
    p = Path(path)
    if p.is_file() and p.read_text() == text:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _souffle_rows(outdir: Path, rel: str) -> list[tuple[str, ...]]:
    f = outdir / f"{rel}.csv"
    if not f.is_file():
        return []
    return sorted(tuple(line.split("\t")) for line in f.read_text().splitlines() if line)


def collate(program: Path, souffle: Path, args: list[str]) -> str | None:
    family, dd, prov = "clang", None, None
    tus, ifaces, sees = [], [], []
    for a in args:
        key, _, value = a.partition("=")
        if key == "--family":
            family = value
        elif key == "--dd":
            dd = value
        elif key == "--prov":
            prov = value
        elif key == "--tu":
            kind, owner, src, ddi, out, mm = value.split(",")
            tus.append(
                {"kind": kind, "owner": owner, "src": src, "ddi": ddi, "out": out, "map": mm}
            )
        elif key == "--iface":
            p, src, vis, ddi, bmi = value.split(",")
            ifaces.append({"provider": p, "src": src, "vis": vis, "ddi": ddi, "bmi": bmi})
        elif key == "--sees":
            u, p = value.split(",")
            sees.append((u, p))
        else:
            return f"collate: unknown argument {a}"
    if not dd or not prov:
        return "collate: --dd and --prov are required"

    work = Path(dd + ".work")
    shutil.rmtree(work, ignore_errors=True)
    facts, outdir = work / "facts", work / "out"
    facts.mkdir(parents=True)
    outdir.mkdir()
    errors: list[str] = []
    provides_rows, requires_rows = set(), set()
    seen = set()
    for unit in [*tus, *ifaces]:
        if unit["src"] in seen:
            continue
        seen.add(unit["src"])
        provides, requires, errs = _read_ddi(unit["ddi"])
        errors += errs
        provides_rows |= {(unit["src"], n, i) for n, i in provides}
        requires_rows |= {(unit["src"], n[0]) for n in requires}
    tables = {
        "scan_provides": provides_rows,
        "scan_requires": requires_rows,
        "collate_tu": {(t["kind"], t["owner"], t["src"], t["out"], t["map"]) for t in tus},
        "collate_iface": {(i["provider"], i["src"], i["vis"], i["bmi"]) for i in ifaces},
        "collate_sees": set(sees),
    }
    for name, rows in tables.items():
        (facts / f"{name}.facts").write_text("".join("\t".join(r) + "\n" for r in sorted(rows)))
    r = subprocess.run(
        [str(souffle), "-F", str(facts), "-D", str(outdir), str(program)],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode:
        return f"collate: Soufflé failed:\n{r.stderr}"
    errors += [f"{k}: {d}" for k, d in _souffle_rows(outdir, "collate_error")]
    if errors:
        shutil.rmtree(work, ignore_errors=True)
        return "C++ modules:\n  " + "\n  ".join(sorted(set(errors)))

    needs: dict[str, list[str]] = {}
    for out, bmi in _souffle_rows(outdir, "collate_need"):
        needs.setdefault(out, []).append(bmi)
    maps: dict[str, list[tuple[str, str]]] = {t["map"]: [] for t in tus}
    for m, n, bmi in _souffle_rows(outdir, "collate_map"):
        maps[m].append((n, bmi))
    via = _souffle_rows(outdir, "collate_via")
    if family == "gcc":
        # GCC writes the CMI of the module a compile provides to where its
        # mapper says: the BMI's own path, or, for an object compile of an
        # interface unit, a side file (not the BMI another action builds).
        for t in tus:
            for src, name, _ in sorted(provides_rows):
                if src == t["src"]:
                    maps[t["map"]].append(
                        (name, t["out"] if t["kind"] == "bmi" else t["out"] + ".gcm")
                    )
    shutil.rmtree(work, ignore_errors=True)
    _write_if_changed(dd, _dd_text(tus, needs, family))
    for m, rows in maps.items():
        _write_if_changed(m, _modmap_text(rows, family))
    edges = _explain_edges(tus, ifaces, via, needs)
    record = {
        "edges": edges,
        "family": family,
        "units": sorted(tus, key=lambda t: t["out"]),
        "interfaces": sorted(ifaces, key=lambda i: i["src"]),
        "provides": sorted(provides_rows),
        "requires": sorted(requires_rows),
        "resolved": [
            {"src": a, "name": n, "provider_src": s, "provider": p, "vis": v}
            for a, n, s, p, v in via
        ],
        "needs": {out: sorted(b) for out, b in sorted(needs.items())},
    }
    _write_if_changed(prov, json.dumps(record, indent=1, sort_keys=True) + "\n")
    return None


def _explain_edges(tus, ifaces, via, needs) -> list[dict[str, str]]:
    """One line per dynamic edge (a BMI an output reads): why it is there."""
    bmi_of = {i["src"]: (i["bmi"], i["provider"], i["vis"]) for i in ifaces}
    src_of = {t["out"]: t["src"] for t in tus}
    direct: dict[tuple[str, str], str] = {}
    for a, n, s, p, v in via:
        bmi = bmi_of[s][0]
        for t in tus:
            if t["src"] == a:
                direct[(t["out"], bmi)] = (
                    f"{a} imports {n}, provided by {s} of target {p} ({v} interface)"
                )
    edges = []
    for out, bmis in sorted(needs.items()):
        for bmi in sorted(bmis):
            because = direct.get((out, bmi))
            if because is None:
                because = f"{src_of.get(out, out)} reads it through the modules it imports (a BMI loads the BMIs it was built from)"
            edges.append({"output": out, "input": bmi, "because": because})
    return edges


def main(program: Path, souffle: Path, args: list[str]) -> str | None:
    return collate(program, souffle, args)
