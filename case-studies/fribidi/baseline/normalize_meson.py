# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Normalize a Meson build directory's Ninja graph into the case studies' records.

The same record format as the CMake baselines (case-studies/halide/baseline/
normalize.py, whose Ninja parser, command decomposition, classification and path
canonicalization this reuses): one JSON record per build edge, machine paths
replaced by placeholders ($SRC, $BUILD, $HOME, ...), commands decomposed into
steps. What is Meson-specific is the target attribution, which comes from
`meson introspect --targets` (meson-info/intro-targets.json) instead of CMake's
File API:

  * an edge whose output is a target's file (`filename`) belongs to it;
  * an edge whose output lies in a target's private directory
    (`<filename>.p/`: objects, the shared library's .symbols) belongs to it;
  * the rest (phonies, Meson's bookkeeping) are attributed by the shared
    normalizer's consumer/input propagation, or not at all.

Records carry `target` (the Meson target name, suffixed with its id's type when
names collide, see labels()), `target_id` (Meson's id), `target_type`,
`machine` (Meson's build/host machine of the target's compilers) and `dir` (the
source directory whose meson.build defines the target).

Meson's `meson --internal exe --capture F -- cmd` wrapper is decomposed into its
command with stdout F, and the ccache launcher Meson adds on its own becomes a
step `wrapper`; Meson's own steps (symbolextractor, regenerate, test, install,
...) classify as meta and are excluded (kept as identity-only records).

  normalize_meson.py --build DIR --src DIR [--variant NAME]
                     [--build-ninja FILE] [--meson-info DIR] -o OUT.jsonl

Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys


def _load_shared_normalizer():
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "halide", "baseline", "normalize.py"
    )
    spec = importlib.util.spec_from_file_location("case_study_normalize", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod


N = _load_shared_normalizer()

# Everything is one phase; Meson's own bookkeeping is out of scope.
SCOPE = {
    "rule": [
        {"kind": "regen", "exclude": "meson bookkeeping (regenerate)"},
        {"kind": "meta", "exclude": "meson bookkeeping"},
        {"phase": 1},
    ]
}


def load_targets(info_dir: str) -> list[dict]:
    with open(os.path.join(info_dir, "intro-targets.json")) as f:
        return json.load(f)


def labels(targets: list[dict]) -> dict[str, str]:
    """{Meson id: label}: the target's name, or name@<id suffix> (exe, sha, sta,
    cus, ...) when several targets share it (fribidi names both its library and
    its CLI `fribidi`; a gen.tab tool and the custom target running it share
    theirs)."""
    count: dict[str, int] = {}
    for t in targets:
        count[t["name"]] = count.get(t["name"], 0) + 1
    return {
        t["id"]: t["name"] if count[t["name"]] == 1 else t["name"] + "@" + t["id"].rsplit("@", 1)[1]
        for t in targets
    }


def attribution(
    nf: N.NinjaFile, norm: N.Normalizer, targets: list[dict], src: str
) -> tuple[dict[str, str], dict[str, str], dict[str, dict]]:
    """({normalized output: target label}, {label: dir}, {label: record fields})."""
    label = labels(targets)
    by_file: dict[str, str] = {}
    private: list[tuple[str, str]] = []
    tdirs: dict[str, str] = {}
    info: dict[str, dict] = {}
    for t in targets:
        lb = label[t["id"]]
        for f in t.get("filename", []):
            f = os.path.normpath(f)
            by_file[f] = lb
            private.append((f + ".p" + os.sep, lb))
        tdirs[lb] = os.path.relpath(os.path.dirname(t["defined_in"]), src)
        machines = {s["machine"] for s in t.get("target_sources", []) if s.get("machine")}
        info[lb] = {"target_id": t["id"], "target_type": t["type"]}
        if machines:
            info[lb]["machine"] = ",".join(sorted(machines))
    private.sort(key=lambda p: -len(p[0]))
    tmap: dict[str, str] = {}
    for e in nf.edges:
        for o in e.outputs + e.implicit_outputs:
            a = norm.absp(o)
            lb = by_file.get(a) or next((lb for p, lb in private if a.startswith(p)), None)
            if lb is not None:
                tmap[norm.cpath(o)] = lb
    return tmap, tdirs, info


def _build_relative_includes(argv: list[str]) -> list[str]:
    """Meson writes include directories relative to the build directory (-Ilib,
    -I.); the shared normalizer canonicalizes only those that climb out of it."""
    out = []
    for t in argv:
        rel = t[2:]
        if t.startswith("-I") and rel and not rel.startswith(("$", "/")):
            rel = os.path.normpath(rel)
            t = "-I$BUILD" + ("" if rel == "." else "/" + rel)
        out.append(t)
    return out


def normalize_meson(
    build: str,
    src: str,
    variant: str | None = None,
    build_ninja: str | None = None,
    meson_info: str | None = None,
) -> list[dict]:
    """The records of the Meson build directory `build` set up for `src`. The
    graph and the introspection data are read from build/build.ninja and
    build/meson-info unless given (a copied graph, a test fixture)."""
    build, src = os.path.abspath(build), os.path.abspath(src)
    nf = N.parse_ninja(build_ninja or os.path.join(build, "build.ninja"))
    roots = N.default_roots(src, build, None)
    norm = N.Normalizer(nf, build, roots)
    targets = load_targets(meson_info or os.path.join(build, "meson-info"))
    tmap, tdirs, info = attribution(nf, norm, targets, src)
    recs = N.normalize(nf, build, roots, None, SCOPE, variant, tmap, tdirs, set())
    for r in recs:
        if r.get("target") in info:
            r.update(info[r["target"]])
        if "argv" in r:
            r["argv"] = _build_relative_includes(r["argv"])
        for st in r.get("steps", []):
            st["argv"] = _build_relative_includes(st["argv"])
    return [N.slim(r) for r in recs]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--build", required=True, help="the Meson build directory")
    ap.add_argument("--src", required=True, help="the source directory it was set up for")
    ap.add_argument("--variant")
    ap.add_argument("--build-ninja", help="the graph, if not BUILD/build.ninja")
    ap.add_argument("--meson-info", help="the introspection directory, if not BUILD/meson-info")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args(argv)
    recs = normalize_meson(args.build, args.src, args.variant, args.build_ninja, args.meson_info)
    N.write_jsonl(recs, args.output)
    print(N.summarize(recs), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
