# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Assemble a wheel from a configured and built buildlog build directory.

A prototype of the packaging step (case-studies/nanobind/README.md,
"Wheels"): buildlog derives what a wheel of one configuration contains
(python_wheel_file: the package tree it staged) and its tag
(python_wheel: from the interpreter probe, the project's stable-ABI policy
and the configuration's platform). The metadata is the author's
pyproject.toml [project] table (PEP 621), which this module turns into
core metadata 2.1 as scikit-build-core does. Only static metadata is
supported (no `dynamic`).

    python -m buildlog_python.wheel -C BUILDDIR --config release-cp313 \
        --pyproject SRC/pyproject.toml --out DIR
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import re
import sys
import tomllib
import zipfile
from pathlib import Path

GENERATOR = "buildlog-python 0.1.0"
LICENSE_GLOBS = ("LICEN[CS]E*", "COPYING*", "NOTICE*", "AUTHORS*")
# Reproducible archives: every entry gets this timestamp.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class WheelError(Exception):
    pass


def _rows(builddir: Path, rel: str) -> list[list[str]]:
    path = builddir / "souffle" / "facts" / f"{rel}.csv"
    if not path.is_file():
        raise WheelError(f"{path}: missing (configure the project with buildlog-python first)")
    with path.open(newline="") as f:
        return [r for r in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)]


def wheel_facts(builddir: Path, decl: str) -> tuple[str, list[tuple[str, Path]]]:
    """The tag and the (archive path, file) list of configuration decl."""
    tags = sorted({"-".join(r[2:5]) for r in _rows(builddir, "python_wheel") if r[0] == decl})
    if len(tags) != 1:
        raise WheelError(f"configuration {decl!r}: expected one wheel tag, found {tags or 'none'}")
    files = []
    for d, rel, path in _rows(builddir, "python_wheel_file"):
        if d != decl:
            continue
        if not path.startswith("out:"):
            raise WheelError(f"{rel}: not a build output ({path})")
        files.append((rel, builddir / "souffle" / path[4:]))
    missing = [str(p) for _, p in files if not p.is_file()]
    if missing:
        raise WheelError(f"not built (run ninja): {', '.join(missing)}")
    return tags[0], sorted(files)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def core_metadata(pyproject: Path) -> tuple[str, str, str, list[Path]]:
    """(distribution name, version, METADATA text, license files)."""
    project = tomllib.loads(pyproject.read_text()).get("project")
    if not isinstance(project, dict):
        raise WheelError(f"{pyproject}: no [project] table")
    if project.get("dynamic"):
        raise WheelError(f"{pyproject}: dynamic metadata is not supported: {project['dynamic']}")
    for key in ("name", "version"):
        if not isinstance(project.get(key), str):
            raise WheelError(f"{pyproject}: [project] {key} must be a string")
    root = pyproject.parent
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {project['name']}",
        f"Version: {project['version']}",
    ]
    if "description" in project:
        lines.append(f"Summary: {project['description']}")
    for field, key in (("Author", "authors"), ("Maintainer", "maintainers")):
        for person in project.get(key, []):
            name, email = person.get("name"), person.get("email")
            if email:
                lines.append(
                    f"{field}-Email: {name} <{email}>" if name else f"{field}-Email: {email}"
                )
            elif name:
                lines.append(f"{field}: {name}")
    lines += [f"Classifier: {c}" for c in project.get("classifiers", [])]
    lines += [f"Project-URL: {k}, {v}" for k, v in project.get("urls", {}).items()]
    if "requires-python" in project:
        lines.append(f"Requires-Python: {project['requires-python']}")
    lines += [f"Requires-Dist: {r}" for r in project.get("dependencies", [])]
    body = ""
    readme = project.get("readme")
    if isinstance(readme, str):
        readme = {"file": readme}
    if isinstance(readme, dict):
        if "file" in readme:
            body = (root / readme["file"]).read_text()
            suffix = Path(readme["file"]).suffix.lower()
            kind = {".md": "text/markdown", ".rst": "text/x-rst"}.get(suffix, "text/plain")
        else:
            body = readme.get("text", "")
            kind = "text/plain"
        lines.append(f"Description-Content-Type: {readme.get('content-type', kind)}")
    text = "\n".join(lines) + "\n"
    if body:
        text += "\n" + body
    licenses = sorted({p for g in LICENSE_GLOBS for p in root.glob(g) if p.is_file()})
    return project["name"], project["version"], text, licenses


def _record_line(arc: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{arc},sha256={digest},{len(data)}"


def build_wheel(builddir: Path, decl: str, pyproject: Path, out: Path) -> Path:
    tag, files = wheel_facts(builddir, decl)
    name, version, metadata, licenses = core_metadata(pyproject)
    dist = f"{_normalize(name)}-{version}"
    info = f"{dist}.dist-info"
    wheel_text = f"Wheel-Version: 1.0\nGenerator: {GENERATOR}\nRoot-Is-Purelib: false\nTag: {tag}\n"
    entries: list[tuple[str, bytes, int]] = []
    for rel, path in files:
        mode = path.stat().st_mode & 0o777
        entries.append((rel, path.read_bytes(), mode))
    entries.append((f"{info}/METADATA", metadata.encode(), 0o644))
    entries.append((f"{info}/WHEEL", wheel_text.encode(), 0o644))
    for lic in licenses:
        entries.append((f"{info}/licenses/{lic.name}", lic.read_bytes(), 0o644))
    record = "".join(_record_line(a, d) + "\n" for a, d, _ in entries) + f"{info}/RECORD,,\n"
    entries.append((f"{info}/RECORD", record.encode(), 0o644))
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{dist}-{tag}.whl"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data, mode in entries:
            zi = zipfile.ZipInfo(arc, ZIP_DATE)
            zi.external_attr = (0o100000 | mode) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, data)
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m buildlog_python.wheel")
    ap.add_argument("-C", "--builddir", type=Path, required=True)
    ap.add_argument("--config", required=True, help="a declared configuration (config_decl)")
    ap.add_argument("--pyproject", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        path = build_wheel(args.builddir, args.config, args.pyproject.expanduser(), args.out)
    except WheelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
