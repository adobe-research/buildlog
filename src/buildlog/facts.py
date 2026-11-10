# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import bisect
import csv
import json
import os
import re
import shutil
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import conditions
from .errors import BuildlogError, SpecError, read_toml, toml_error
from .layers import _code_only

if TYPE_CHECKING:
    from .modules import ModuleFacts

DECL_RE = re.compile(r"(?:^|(?<=\s))\.decl\s+([A-Za-z_][\w.]*)\s*\(([^)]*)\)")
DECL_START_RE = re.compile(r"(?:^|(?<=\s))\.decl\b")


def iter_decls(text: str, origin: str = "") -> Iterable[tuple[str, str]]:
    """(relation, attribute list) of every .decl, spanning lines, ignoring comments."""
    code = _code_only(text)
    found = list(DECL_RE.finditer(code))
    if len(found) != len(DECL_START_RE.findall(code)):
        raise SpecError(
            f"{origin}: a .decl could not be parsed (expected .decl name(attr:type, ...))"
        )
    for m in found:
        yield m.group(1), " ".join(m.group(2).split())


INPUT_RE = re.compile(r"^\s*\.input\s+(.+)")
OUTPUT_RE = re.compile(r"^\s*\.output\s+(.+)")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# A generator's ${gen} (its directory) is a placeholder, not a variable.
PLACEHOLDER_NAMES = frozenset({"gen"})
VAR_ESC_RE = re.compile(r"\$\$\{|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ProgramSchema:
    fields: dict[str, list[str]] = field(default_factory=dict)
    types: dict[str, list[str]] = field(default_factory=dict)
    inputs: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    user_inputs: set[str] = field(default_factory=set)
    # Filled only by discovery; spec and package files may not supply them.
    probe_results: set[str] = field(default_factory=set)
    driver_inputs: set[str] = field(default_factory=set)
    author_relations: set[str] | None = None
    author_inputs: set[str] | None = None
    module_facts: dict[Path, ModuleFacts] = field(default_factory=dict)


def _split_names(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def schema_from_rules(rule_dirs: Iterable[Path]) -> ProgramSchema:
    schema = ProgramSchema()
    for directory in rule_dirs:
        for path in sorted(directory.rglob("*.dl")):
            text = path.read_text()
            for rel, body in iter_decls(text, str(path)):
                names: list[str] = []
                types: list[str] = []
                for part in _split_names(body):
                    if ":" in part:
                        name, typ = part.split(":", 1)
                        names.append(name.strip())
                        types.append(typ.strip())
                    else:
                        names.append(part.strip())
                        types.append("symbol")
                schema.fields.setdefault(rel, names)
                schema.types.setdefault(rel, types)
            for line in text.splitlines():
                input_match = INPUT_RE.match(line)
                if input_match:
                    schema.inputs.update(_split_names(input_match.group(1)))
                output_match = OUTPUT_RE.match(line)
                if output_match:
                    schema.outputs.update(_split_names(output_match.group(1)))
    return schema


def read_rows(path: Path) -> list[tuple[str, ...]]:
    if not path.exists():
        return []
    with path.open(newline="") as file:
        return [
            tuple(row) for row in csv.reader(file, delimiter="\t", quoting=csv.QUOTE_NONE) if row
        ]


def write_rows(path: Path, rows: Iterable[Iterable[str]], shuffle_seed: int | None = None) -> None:
    vals = sorted({tuple(str(value) for value in row) for row in rows})
    if shuffle_seed is not None:
        import random

        random.Random(shuffle_seed).shuffle(vals)
    # Soufflé reads fact files verbatim (no quoting or escapes), so values are
    # written raw; only the delimiter and line breaks cannot be represented.
    for row in vals:
        for value in row:
            if any(c in value for c in "\t\n\r"):
                raise SpecError(f"{path.stem}: value {value!r} contains a tab or line break")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as file:
        file.writelines("\t".join(row) + "\n" for row in vals)


def tuple_key(rel: str, row: tuple[str, ...]) -> str:
    return json.dumps([rel, list(row)], sort_keys=True)


MISSING_ENTRY_POINT = "missing project entry point (create buildlog.toml, or a [tool.buildlog] table in pyproject.toml)"


def _pyproject_table(path: Path) -> dict | None:
    if not path.is_file():
        return None
    tool = read_toml(path).get("tool", {})
    table = tool.get("buildlog") if isinstance(tool, dict) else None
    if table is None:
        return None
    if not isinstance(table, dict):
        raise BuildlogError(f"{path}: [tool.buildlog] must be a table")
    return table


def project_config_file(project: Path) -> Path | None:
    """The project's entry point: buildlog.toml, else pyproject.toml when it
    has a [tool.buildlog] table (the same keys, for a Python project whose
    native part buildlog builds). Both at once is an error."""
    config = project / "buildlog.toml"
    pyproject = project / "pyproject.toml"
    has_table = _pyproject_table(pyproject) is not None
    if config.is_file():
        if has_table:
            raise BuildlogError(
                f"{project}: both buildlog.toml and pyproject.toml [tool.buildlog]; keep one"
            )
        return config
    return pyproject if has_table else None


def read_project_config(config: Path) -> dict:
    """The project keys of an entry point project_config_file returned."""
    if config.name == "pyproject.toml":
        return _pyproject_table(config) or {}
    return read_toml(config)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_vars(project: Path, overrides: dict[str, str] | None = None) -> dict[str, str]:
    repo = _repo_root()
    values = {
        "REPO_ROOT": str(repo),
    }
    config = project_config_file(project)
    candidates: list[Path] = []
    if config is not None:
        candidates.append(config)
        data = read_project_config(config)
        for rel in ("include", "includes"):
            for item in _string_list(data.get(rel, []), f"{config}: {rel}"):
                candidates.append((config.parent / item).resolve())
        for item in _string_list(data.get("spec", []), f"{config}: spec"):
            path = (config.parent / item).resolve()
            if path.suffix == ".toml":
                candidates.append(path)
    for candidate in candidates:
        if candidate.exists():
            data = read_project_config(candidate) if candidate == config else read_toml(candidate)
            for key, value in data.get("vars", {}).items():
                values[key] = str(value)
            if "source_root" in data:
                values["SOURCE_ROOT"] = str(data["source_root"])
    for key in list(values):
        values[key] = os.environ.get(f"BUILDLOG_{key}", values[key])
    values.update(overrides or {})
    for _ in range(4):
        changed = False
        for key, value in list(values.items()):
            expanded = substitute_vars(value, values, "vars") if "${" in value else value
            changed = changed or expanded != value
            values[key] = expanded
        if not changed:
            break
    for key in list(values):
        values[key] = (
            str(Path(values[key]).expanduser()) if values[key].startswith("~") else values[key]
        )
    return values


def package_vars(path: Path, variables: dict[str, str]) -> dict[str, str]:
    """A package's facts file also sees ${PACKAGE_PREFIX}: the prefix the
    package is installed under (<prefix>/share/buildlog/<name>/), so facts
    about the package's own files are stated relative to it, as CPS's
    @prefix@ Elsewhere it is unset."""
    for d in path.resolve().parents:
        if (d / "manifest.toml").is_file():
            if d.parent.name == "buildlog" and d.parent.parent.name == "share":
                return {**variables, "PACKAGE_PREFIX": str(d.parent.parent.parent)}
            break
    return variables


def substitute_vars(value: str, variables: dict[str, str], origin: str) -> str:
    """${NAME} is the variable NAME; $${ is a literal ${ (for a configured
    file's own ${var}, e.g. pkg-config's exec_prefix = "$${exec_prefix}")."""

    def replace(match: re.Match[str]) -> str:
        if match.group(0).startswith("$$"):
            return match.group(0)[1:]
        name = match.group(1)
        if name not in variables and name in PLACEHOLDER_NAMES:
            return match.group(0)
        if name not in variables:
            raise SpecError(f"{origin}: unresolved variable ${{{name}}}")
        return variables[name]

    return VAR_ESC_RE.sub(replace, value)


# A line break cannot be stored in a facts file (one row per line, no
# escapes), so a multi-line TOML string's breaks are stored as U+2028 (LINE
# SEPARATOR). The consumers of text (configure checks' programs, configured
# files' text pieces) turn them back with `text_lines`.
LINE_SEP = "\u2028"


def encode_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\n", LINE_SEP)


def text_lines(value: str) -> str:
    """A stored value as the text it was written as (line breaks restored)."""
    return value.replace(LINE_SEP, "\n")


# Loader warnings (the driver relation spec_warning; core prints them as
# warnings): TOML placement traps the loader notices.
SPEC_WARNING = "spec_warning"

# `-O NAME=VALUE` travels with the -D overrides under this prefix (never a
# ${VAR} name) and becomes a user option_value row.
OPTION_PREFIX = "option:"

# buildlog.toml keys that configure the project, not facts. Its other keys
# are spec facts, so a small project is one file.
PROJECT_KEYS = {
    "vars",
    "source_root",
    "uses",
    "spec",
    "include",
    "includes",
    "author_decides",
    "presets",
}

# A user input may be one of buildlog.toml's [presets.<name>] tables
# (`--preset NAME`): shipped example user choices. It travels as a user-file
# path with this mark, so it is recorded and regenerated like a --user file.
PRESET_MARK = "#preset:"


def preset_path(project: Path, name: str) -> Path:
    config = project_config_file(project)
    if config is None:
        raise BuildlogError(f"{project / 'buildlog.toml'}: {MISSING_ENTRY_POINT}")
    presets = read_project_config(config).get("presets", {})
    if not isinstance(presets, dict) or not isinstance(presets.get(name), dict):
        known = ", ".join(sorted(presets)) if isinstance(presets, dict) else ""
        raise BuildlogError(
            f"--preset {name}: {config} has no [presets.{name}] (has: {known or 'none'})"
        )
    return Path(f"{config.resolve()}{PRESET_MARK}{name}")


def user_file_parts(path: Path) -> tuple[Path, tuple[str, ...]]:
    """A user input's file, and the table in it (a preset's), if any."""
    text = str(path)
    if PRESET_MARK in text:
        base, name = text.split(PRESET_MARK, 1)
        return Path(base), ("presets", name)
    return path, ()


def is_relation_key(key: str) -> bool:
    return not key.startswith("_") and key not in PROJECT_KEYS


def _string_list(value: object, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BuildlogError(f"{where} must be a list of strings")
    return list(value)


@dataclass(frozen=True)
class ProjectSpec:
    config: Path
    facts: list[Path]
    tomls: list[Path]
    dls: list[Path]
    includes: list[Path]
    author_decides: dict[str, str]

    @property
    def inputs(self) -> list[Path]:
        return [self.config, *self.includes, *self.facts, *self.tomls, *self.dls]


def project_spec(project: Path) -> ProjectSpec:
    """Read the explicit project entry point.

    buildlog.toml is the only conventional file. It names all authored spec
    inputs in `spec = [...]`; `include = [...]` is for shared config such as
    vars/source_root and does not supply facts.
    """
    config = project_config_file(project)
    if config is None:
        raise BuildlogError(f"{project / 'buildlog.toml'}: {MISSING_ENTRY_POINT}")
    data = read_project_config(config)
    author_decides: dict[str, str] = {}
    raw_author_decides = data.get("author_decides", {})
    if raw_author_decides is None:
        raw_author_decides = {}
    if not isinstance(raw_author_decides, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.strip()
        for k, v in raw_author_decides.items()
    ):
        raise BuildlogError(
            f"{config}: author_decides must be a table of relation = non-empty reason strings"
        )
    author_decides = {str(k): str(v) for k, v in raw_author_decides.items()}
    facts: list[Path] = []
    tomls: list[Path] = []
    dls: list[Path] = []
    for item in _string_list(data.get("spec", []), f"{config}: spec"):
        path = (config.parent / item).resolve()
        if not path.is_file():
            raise BuildlogError(f"{config}: spec entry {item!r} does not exist")
        if path.suffix == ".facts":
            facts.append(path)
        elif path.suffix == ".toml":
            tomls.append(path)
        elif path.suffix == ".dl":
            dls.append(path)
        else:
            raise BuildlogError(f"{config}: spec entry {item!r} must end in .toml, .facts or .dl")
    if config.name != "pyproject.toml" and any(is_relation_key(k) for k in data):
        tomls.insert(0, config.resolve())
    includes: list[Path] = []
    for key in ("include", "includes"):
        for item in _string_list(data.get(key, []), f"{config}: {key}"):
            path = (config.parent / item).resolve()
            if not path.is_file():
                raise BuildlogError(f"{config}: {key} entry {item!r} does not exist")
            includes.append(path)
    return ProjectSpec(config.resolve(), facts, tomls, dls, includes, author_decides)


def spec_files(project: Path) -> tuple[list[Path], list[Path]]:
    """The project's explicit `.facts` and TOML spec files."""
    spec = project_spec(project)
    return spec.facts.copy(), spec.tomls.copy()


def project_dl_files(project: Path) -> list[Path]:
    return project_spec(project).dls.copy()


def spec_relations(project: Path) -> dict[str, str]:
    """Relation name -> first spec file that supplies it."""
    out: dict[str, str] = {}
    spec = project_spec(project)
    project_declared: set[str] = set()
    for path in spec.dls:
        project_declared.update(rel for rel, _ in iter_decls(path.read_text(), str(path)))
    facts, tomls = spec.facts, spec.tomls
    for path in facts:
        if path.stem not in project_declared:
            out.setdefault(path.stem, str(path))
    for path in tomls:
        for key in read_toml(path):
            if (
                is_relation_key(key)
                and key not in (DEFAULTS_KEY, PROFILE_KEY)
                and key not in project_declared
            ):
                out.setdefault(key, str(path))
    return out


def _toml_value_lines(text: str) -> dict[str, list[int]]:
    """Line of each top-level key's value, and of each element when it is an array.

    A small scanner (TOML has no position API): it tracks strings, comments
    and brackets from `key = ` to the end of the value.
    """
    out: dict[str, list[int]] = {}
    key_re = re.compile(r'[ \t]*("?)([A-Za-z0-9_.-]+)\1[ \t]*=[ \t]*')
    pos, line_no = 0, 1
    while pos < len(text):
        end = text.find("\n", pos)
        end = len(text) if end < 0 else end
        line = text[pos:end]
        if line.strip().startswith("["):
            break  # keys after a table header belong to that table
        m = key_re.match(line)
        if not m:
            pos, line_no = end + 1, line_no + 1
            continue
        i, ln = pos + m.end(), line_no
        lines = [ln]
        depth, elem_expected = 0, False
        while i < len(text):
            ch = text[i]
            if ch == "\n":
                ln += 1
                i += 1
                if depth == 0:
                    break
                continue
            if ch == "#":
                while i < len(text) and text[i] != "\n":
                    i += 1
                continue
            if depth == 1 and elem_expected and ch not in " \t\r,]":
                lines.append(ln)
                elem_expected = False
            if ch in "\"'":
                triple = text.startswith(ch * 3, i)
                close = ch * 3 if triple else ch
                j = i + len(close)
                while j < len(text) and not text.startswith(close, j):
                    j += 2 if (ch == '"' and text[j] == "\\") else 1
                ln += text.count("\n", i, j)
                i = j + len(close)
                continue
            if ch in "[{":
                depth += 1
                if depth == 1 and ch == "[":
                    elem_expected = True
            elif ch in "]}":
                depth -= 1
            elif ch == "," and depth == 1:
                elem_expected = True
            i += 1
        out[m.group(2)] = lines
        line_no = ln
        pos = i
    return out


def _suggest(name: str, choices: dict[str, object] | set[str]) -> str | None:
    import difflib

    vals = list(choices)
    found = difflib.get_close_matches(name, vals, n=1)
    return found[0] if found else None


_TABLE_HEADER = re.compile(r"\[\[?\s*[A-Za-z_]")


def _unquote_header(line: str) -> str:
    """A table header with its quoted keys unquoted: [imported."Qt6::Core"]
    is the table imported.Qt6::Core (a key holding a dot stays ambiguous)."""
    if line.startswith("[") and '"' in line:
        return re.sub(r'"([^"]*)"', r"\1", line)
    return line


class _LineIndex:
    """Where a file's table headers are, so _table_key_line looks up a header
    and scans only its table instead of the file from the top."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.size = len(lines)
        self.first: dict[str, int] = {}
        self.headers: list[tuple[int, str]] = []
        for no, line in enumerate(lines, 1):
            stripped = _unquote_header(line.strip())
            if stripped.startswith("["):
                self.first.setdefault(stripped, no)
                if not line[:1].isspace() and _TABLE_HEADER.match(stripped):
                    self.headers.append((no, stripped))
        self.ends: dict[str, int] = {}

    def end(self, header: str, start: int) -> int:
        """The first line after `start` that closes the table `header`: an
        unindented header other than `header` itself (or one past the end)."""
        if header not in self.ends:
            i = bisect.bisect_right(self.headers, (start, "\uffff"))
            end = self.size + 1
            for no, stripped in self.headers[i:]:
                if stripped != header:
                    end = no
                    break
            self.ends[header] = end
        return self.ends[header]


_LINE_INDEXES: dict[int, _LineIndex] = {}


def _line_index(lines: list[str]) -> _LineIndex:
    index = _LINE_INDEXES.get(id(lines))
    if index is None or index.lines is not lines or index.size != len(lines):
        if len(_LINE_INDEXES) > 64:
            _LINE_INDEXES.clear()
        index = _LINE_INDEXES[id(lines)] = _LineIndex(lines)
    return index


def _table_key_line(lines: list[str], table: str, key: str, default: int) -> int:
    """The line of `key` in the table headed `[table]` (a subject table's
    header is `[rel.subject]`, and `[rel.subject.key]` states the key as a
    table); a multi-line array's rows are not headers.

    This is the first match of a scan from the top that enters the table at
    its first `[table]` line, returns a `[table.key]` line anywhere before the
    table ends, and stops at the next unindented header; the index gives the
    same answer without scanning the rest of the file."""
    index = _line_index(lines)
    header = f"[{table}]"
    start = index.first.get(header)
    keyed = index.first.get(f"[{table}.{key}]")
    if start is None:
        return keyed if keyed is not None else default
    if keyed is not None and keyed < start:
        return keyed
    end = index.end(header, start)
    pat = re.compile(rf'\s*"?{re.escape(key)}"?\s*=')
    for no in range(start + 1, end):
        if no == keyed:
            return no
        line = lines[no - 1]
        if _unquote_header(line.strip()) != header and pat.match(line):
            return no
    return keyed if keyed == end else default


# Author linkage constraints need a reason (the last column), so that
# authors leave linkage to the user unless they can say why not.
LINKAGE_KEY = "linkage"
# The kinds a built library no longer has: its linkage is a choice.
_FIXED_KINDS = {"static_library": "static", "shared_library": "shared"}
_REASONED = {
    "linkage_require": 'linkage = { require = "static", why = "..." } under [target.X]',
    "dep_linkage": "dep_linkage rows are [dependent, library, linkage, why]",
}


# First-column names that all name a target (or a library/owner of
# targets): a key under [target.X] whose relation's first column is one of
# these is about X, whatever the column is called.
_TARGET_ROLES = {"t", "target", "lib", "name", "owner", "g"}


def _role(column: str) -> str:
    return "target" if column in _TARGET_ROLES else column


def _is_scalar(v: object) -> bool:
    return isinstance(v, str | int | float | bool)


def _fill_ranks(
    rel: str, values: list[object | None], rank_stack: list[int], schema: ProgramSchema, origin: str
) -> list[object]:
    types = schema.types.get(rel, [])
    holes = [i for i, v in enumerate(values) if v is None]
    number_holes = [i for i in holes if i < len(types) and types[i] in {"number", "unsigned"}]
    if len(holes) != len(number_holes):
        raise SpecError(f"{origin}: {rel} does not have values for all non-number columns")
    if len(number_holes) > len(rank_stack):
        raise SpecError(f"{origin}: {rel} has too many unfilled number columns for list ranks")
    out = list(values)
    for col, rank in zip(number_holes, rank_stack[-len(number_holes) :], strict=False):
        out[col] = rank
    if any(v is None for v in out):
        raise SpecError(f"{origin}: {rel} rank inference is ambiguous")
    return out  # type: ignore[return-value]


def _expand_value(
    rel: str,
    prefix: list[object],
    value: object,
    schema: ProgramSchema,
    origin: str,
    ranks: list[int] | None = None,
) -> list[list[object]]:
    """Expand a grouped TOML value for relation rel with prefix columns bound."""
    ranks = [] if ranks is None else ranks
    fields = schema.fields.get(rel, [])
    out: list[list[object]] = []

    def complete(parts: list[object]) -> list[object]:
        vals: list[object | None] = [*prefix, *parts]
        while len(vals) < len(fields):
            vals.append(None)
        if len(vals) > len(fields):
            raise SpecError(f"{origin}: {rel} has too many values")
        return _fill_ranks(rel, vals, ranks, schema, origin)

    def complete_scalar(scalar: object, rr: list[int]) -> list[object]:
        vals: list[object | None] = [*prefix, *([None] * (len(fields) - len(prefix)))]
        types = schema.types.get(rel, [])
        for i in range(len(prefix), len(fields)):
            if i >= len(types) or types[i] not in {"number", "unsigned"}:
                vals[i] = scalar
                break
        else:
            vals.append(scalar)
        return _fill_ranks(rel, vals, rr, schema, origin)

    if _is_scalar(value):
        return [complete([value])]
    if isinstance(value, list):
        for i, item in enumerate(value):
            rr = [*ranks, i]
            if isinstance(item, list):
                # A nested scalar list fills a rank column and the remaining
                # data column; a nested list of rows fills two rank columns.
                if item and all(_is_scalar(x) for x in item):
                    need = len(fields) - len(prefix)
                    if need >= 2 and sum(
                        1
                        for t in schema.types.get(rel, [])[len(prefix) :]
                        if t in {"number", "unsigned"}
                    ):
                        for j, scalar in enumerate(item):
                            out.append(
                                _fill_ranks(
                                    rel,
                                    [*prefix, *([None] * (need - 1)), scalar],
                                    [*rr, j],
                                    schema,
                                    origin,
                                )
                            )
                    else:
                        out.append(complete(list(item)))
                else:
                    out.extend(_expand_value(rel, prefix, item, schema, origin, rr))
            elif isinstance(item, dict):
                vals: list[object | None] = [*prefix, *([None] * (len(fields) - len(prefix)))]
                for k, v in item.items():
                    if k not in fields:
                        raise SpecError(f"{origin}: {rel} has no column {k}")
                    vals[fields.index(k)] = v
                out.append(_fill_ranks(rel, vals, rr, schema, origin))
            else:
                out.append(complete_scalar(item, rr))
        return out
    if isinstance(value, dict):
        # Map form: key fills the next column after the prefix; scalar/list
        # values fill the rest.
        for k, v in value.items():
            vals = [k]
            if isinstance(v, list):
                for i, item in enumerate(v):
                    if isinstance(item, list):
                        out.append(
                            _fill_ranks(rel, [*prefix, *vals, *item], [*ranks, i], schema, origin)
                        )
                    else:
                        # The item fills the first data column after the
                        # key (word_list = { L = ["-a", "-b"] }: name, rank,
                        # word); a number column left is the item's rank.
                        row: list[object | None] = [
                            *prefix,
                            *vals,
                            *([None] * (len(fields) - len(prefix) - 1)),
                        ]
                        types = schema.types.get(rel, [])
                        for col in range(len(prefix) + 1, len(fields)):
                            if col >= len(types) or types[col] not in {"number", "unsigned"}:
                                row[col] = item
                                break
                        out.append(_fill_ranks(rel, row, [*ranks, i], schema, origin))
            elif isinstance(v, dict):
                out.extend(_expand_value(rel, [*prefix, k], v, schema, origin, ranks))
            else:
                out.append(complete([*vals, v]))
        return out
    raise SpecError(f"{origin}: unsupported TOML value for {rel}")


FACT_STMT_RE = re.compile(rf"^\s*({r'[A-Za-z_][\w.]*'})\s*\((.*)\)\s*\.\s*(?://.*)?$")


def _split_args(text: str) -> list[str] | None:
    args: list[str] = []
    cur = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            cur.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            cur.append(ch)
        elif ch == ",":
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if in_string:
        return None
    if cur or text.strip() == "":
        args.append("".join(cur).strip())
    out: list[str] = []
    for arg in args:
        if len(arg) >= 2 and arg[0] == '"' and arg[-1] == '"':
            out.append(bytes(arg[1:-1], "utf-8").decode("unicode_escape"))
        elif re.fullmatch(r"-?\d+", arg):
            out.append(arg)
        else:
            return None
    return out


def project_dl_fact_rows(
    paths: list[Path],
) -> tuple[dict[str, set[tuple[str, ...]]], dict[str, list[str]]]:
    rows: dict[str, set[tuple[str, ...]]] = {}
    origins: dict[str, list[str]] = {}
    for path in paths:
        for no, line in enumerate(path.read_text().splitlines(), 1):
            if ":-" in line or line.lstrip().startswith("."):
                continue
            m = FACT_STMT_RE.match(line)
            if not m:
                continue
            vals = _split_args(m.group(2))
            if vals is None:
                continue
            row = tuple(vals)
            rel = m.group(1)
            rows.setdefault(rel, set()).add(row)
            origins.setdefault(tuple_key(rel, row), []).append(f"{path}:{no}")
    return rows, origins


# Loader keywords of a spec file (not relations): `[defaults.R]` is merged
# into each `[R.x]` subject table of the same file, and `[profile.p]` into
# each subject table that says `profile = "p"`.
DEFAULTS_KEY = "defaults"
PROFILE_KEY = "profile"


@dataclass(frozen=True)
class Profile:
    """A named bundle of subject-table keys (`[profile.p]`), from any spec file."""

    table: dict
    path: Path
    lines: list[str]
    header: str

    def origin(self, key: str) -> str:
        default = next(
            (no for no, line in enumerate(self.lines, 1) if line.strip() == f"[{self.header}]"), 1
        )
        return f"{self.path}:{_table_key_line(self.lines, self.header, key, default)}"


def collect_profiles(paths: Iterable[Path]) -> dict[str, Profile]:
    """Every `[profile.p]` of the project's spec files; a name defined twice is an error."""
    out: dict[str, Profile] = {}
    for path in paths:
        text = path.read_text()
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise BuildlogError(toml_error(path, exc)) from exc
        table = data.get(PROFILE_KEY, {})
        if not isinstance(table, dict):
            raise SpecError(f"{path}: {PROFILE_KEY} must be tables [{PROFILE_KEY}.<name>]")
        for name, body in table.items():
            if not isinstance(body, dict):
                raise SpecError(f"{path}: [{PROFILE_KEY}.{name}] must be a table")
            if name in out:
                raise SpecError(f"{path}: profile {name} is also defined in {out[name].path}")
            out[name] = Profile(body, path, text.splitlines(), f"{PROFILE_KEY}.{name}")
    return out


def _profile_names(value: object, where: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise SpecError(f"{where}: {PROFILE_KEY} must be a name or a list of names")


def load_toml_file(
    path: Path,
    schema: ProgramSchema,
    variables: dict[str, str],
    profiles: dict[str, Profile] | None = None,
    within: tuple[str, ...] = (),
) -> tuple[dict[str, set[tuple[str, ...]]], dict[str, list[str]]]:
    """Rows of a spec TOML file, or of one of its tables (`within`, e.g.
    ("presets", "mac") for buildlog.toml's [presets.mac])."""
    try:
        text = path.read_text()
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BuildlogError(toml_error(path, exc)) from exc
    for part in within:
        data = data.get(part)
        if not isinstance(data, dict):
            raise SpecError(f"{path}: no table [{'.'.join(within)}]")
    prefix = "".join(f"{part}." for part in within)
    if profiles is None:
        profiles = collect_profiles([path]) if not within else {}
    defaults = data.get(DEFAULTS_KEY, {})
    if not isinstance(defaults, dict) or not all(isinstance(v, dict) for v in defaults.values()):
        raise SpecError(f"{path}: {DEFAULTS_KEY} must be tables [{DEFAULTS_KEY}.<relation>]")
    rows: dict[str, set[tuple[str, ...]]] = {}
    origins: dict[str, list[str]] = {}
    lines = text.splitlines()
    table_line: dict[tuple[str, int], int] = {}
    counters: dict[str, int] = {}
    for no, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("[[") and stripped.endswith("]]"):
            rel = stripped[2:-2].strip().removeprefix(prefix)
            index = counters.get(rel, 0)
            counters[rel] = index + 1
            table_line[(rel, index)] = no
        elif stripped.startswith("[") and stripped.endswith("]"):
            header = _unquote_header(stripped)[1:-1].strip().removeprefix(prefix)
            table_line.setdefault((header, 0), no)
    if within:
        head = next((no for no, ln in enumerate(lines, 1) if ln.strip() == f"[{prefix[:-1]}]"), 1)
        value_lines = {k: [_table_key_line(lines, prefix[:-1], k, head)] for k in data}
    else:
        value_lines = _toml_value_lines(text)

    def signature(rel: str) -> str:
        fields = schema.fields.get(rel, [])
        types = schema.types.get(rel, ["symbol"] * len(fields))
        return f"{rel}(" + ", ".join(f"{f}:{t}" for f, t in zip(fields, types, strict=False)) + ")"

    def add_row(rel: str, values: list[object], origin: str) -> None:
        if rel not in schema.inputs and rel not in schema.fields:
            suggestion = _suggest(rel, schema.fields)
            extra = f"; did you mean {suggestion}?" if suggestion else ""
            raise SpecError(f"{origin}: {rel} is not a relation{extra}")
        if rel in schema.probe_results:
            raise SpecError(f"{origin}: {rel} is a probe result; only discovery supplies it")
        if rel in schema.driver_inputs:
            raise SpecError(f"{origin}: {rel} is written by the driver; facts may not supply it")
        if rel not in schema.inputs:
            raise SpecError(f"{origin}: {rel} is supplied only by rules, not spec facts")
        if rel in {"native_kind", "target_kind", "target_kind_when"} and len(values) >= 2:
            kind = values[1]
            if kind in _FIXED_KINDS:
                raise SpecError(
                    f'{origin}: {values[0]}: kind {kind!r} was removed; write kind = "library" '
                    "(the user chooses static or shared), plus "
                    f'linkage = {{ require = "{_FIXED_KINDS[kind]}", why = "..." }} only if '
                    f"{values[0]} cannot be built the other way"
                )
        fields = schema.fields.get(rel, [])
        types = schema.types.get(rel, ["symbol"] * len(fields))
        if rel in _REASONED and fields and len(values) in {len(fields) - 1, len(fields)}:
            why = values[-1] if len(values) == len(fields) else None
            if not isinstance(why, str) or not why.strip():
                raise SpecError(
                    f"{origin}: {rel}{tuple(values)!r} constrains linkage without a reason: "
                    f"{_REASONED[rel]}; linkage is the user's choice unless the project can say "
                    "why it cannot be"
                )
        if fields and len(values) != len(fields):
            raise SpecError(
                f"{origin}: {rel} rows have {len(fields)} columns "
                f"({', '.join(fields)}), this one has {len(values)}; signature {signature(rel)}"
            )
        row: list[str] = []
        for value, typ in zip(values, types, strict=False):
            if typ in {"number", "unsigned"}:
                if not isinstance(value, int):
                    raise SpecError(f"{origin}: {signature(rel)} expects {typ}, got {value!r}")
                if typ == "unsigned" and value < 0:
                    raise SpecError(f"{origin}: {signature(rel)} expects unsigned, got {value!r}")
            if isinstance(value, bool):
                value = "true" if value else "false"
            row.append(encode_text(substitute_vars(str(value), variables, origin)))
        tup = tuple(row)
        rows.setdefault(rel, set()).add(tup)
        origins.setdefault(tuple_key(rel, tup), []).append(origin)

    def warn(origin: str, detail: str) -> None:
        row = ("key captured by a table header", f"{origin}: {detail}")
        rows.setdefault(SPEC_WARNING, set()).add(row)
        origins.setdefault(tuple_key(SPEC_WARNING, row), []).append(origin)

    def resolve_key(subject_rel: str, key: str) -> str:
        # [R.x] k = ... states R_k when R_k accepts facts (the vocabulary's
        # short keys: [target.X] kind, [export.P] version), else k.
        scoped = f"{subject_rel}_{key}"
        if scoped in schema.inputs and scoped not in schema.driver_inputs:
            return scoped
        # A template's table takes what a target's does (core's target_template).
        if subject_rel == "target_template":
            return resolve_key("target", key)
        return key

    def subject_layers(
        subject_rel: str, subject: str, table: dict, line_no: int
    ) -> list[tuple[dict, Callable[[str], str]]]:
        """The subject table, then its profiles, then the file's
        [defaults.R] and that table's profiles: nearest first."""
        header = f"{prefix}{subject_rel}.{subject}"

        def own(key: str) -> str:
            return f"{path}:{_table_key_line(lines, header, key, line_no)}"

        layers: list[tuple[dict, Callable[[str], str]]] = []

        def add(tbl: dict, origin: Callable[[str], str], where: str, seen: tuple[str, ...]) -> None:
            layers.append(({k: v for k, v in tbl.items() if k != PROFILE_KEY}, origin))
            if PROFILE_KEY not in tbl or f"{subject_rel}_{PROFILE_KEY}" in schema.inputs:
                return
            for name in _profile_names(tbl[PROFILE_KEY], where):
                if name in seen:
                    raise SpecError(f"{where}: profile {name} includes itself")
                prof = profiles.get(name)
                if prof is None:
                    known = ", ".join(sorted(profiles)) or "none"
                    raise SpecError(f"{where}: unknown profile {name} (defined: {known})")
                add(prof.table, prof.origin, prof.origin(PROFILE_KEY), (*seen, name))

        add(table, own, own(PROFILE_KEY), ())
        if subject_rel in defaults:
            dflt_header = f"{prefix}{DEFAULTS_KEY}.{subject_rel}"
            dflt_line = table_line.get((f"{DEFAULTS_KEY}.{subject_rel}", 0), 1)

            def dflt(key: str) -> str:
                return f"{path}:{_table_key_line(lines, dflt_header, key, dflt_line)}"

            add(defaults[subject_rel], dflt, dflt(PROFILE_KEY), ())
        return layers

    def expand_named_row(rel: str, subject: str, value: dict, origin: str) -> None:
        """[R.x] key = { col = v, ... }: one row of the key's relation by column
        name (a symbol column left out is empty), and each other key states the
        relation <key's relation>_<key> for the subject (the vocabulary's
        sugar for a thing that takes options, e.g. an add_halide_library)."""
        cols = schema.fields[rel][1:]
        types = schema.types.get(rel, [])
        row: list[object] = [subject]
        for i, col in enumerate(cols, start=1):
            if col in value:
                if not _is_scalar(value[col]):
                    raise SpecError(f"{origin}: {rel}.{col} must be a scalar")
                row.append(value[col])
            elif i < len(types) and types[i] == "symbol":
                row.append("")
            else:
                raise SpecError(f"{origin}: {rel} needs {col}")
        add_row(rel, row, origin)
        for part, v in value.items():
            if part in cols:
                continue
            sub = f"{rel}_{part}"
            if sub not in schema.fields:
                raise SpecError(f"{origin}: {rel} has no key {part}")
            for vals in _expand_value(sub, [subject], v, schema, origin):
                add_row(sub, vals, origin)

    def expand_subject(subject_rel: str, subject: str, table: dict, line_no: int) -> None:
        fields = schema.fields.get(subject_rel, [])
        if subject_rel not in schema.fields:
            raise SpecError(f"{path}:{line_no}: {subject_rel} is not a relation")
        layers = subject_layers(subject_rel, subject, table, line_no)
        # Nearest wins for a scalar or list value (kind, sources, gen_user);
        # a map value (private = {...}, when = {...}) is a bag of
        # properties, and every layer's rows are stated, as CMake appends a
        # directory's properties to a target's.
        entries: list[tuple[str, object, str]] = []
        stated: set[str] = set()
        for tbl, origin_of in layers:
            for key, value in tbl.items():
                if isinstance(value, dict):
                    entries.append((key, value, origin_of(key)))
                elif key not in stated:
                    stated.add(key)
                    entries.append((key, value, origin_of(key)))
        # Subject table states R(subject). If R has more columns, they are
        # filled by same-named scalar keys in the table and not also expanded.
        subject_values: list[object] = [subject]
        consumed: set[str] = set()
        by_key = {key: value for key, value, _ in entries}
        for col_name in fields[1:]:
            if col_name not in by_key:
                raise SpecError(
                    f"{path}:{line_no}: {signature(subject_rel)} subject table needs key {col_name}"
                )
            if isinstance(by_key[col_name], list | dict):
                raise TypeError(f"{path}:{line_no}: {subject_rel}.{col_name} must be scalar")
            subject_values.append(by_key[col_name])
            consumed.add(col_name)
        add_row(subject_rel, subject_values, f"{path}:{line_no}")
        for key, value, origin in entries:
            if key in consumed:
                continue
            if key == LINKAGE_KEY and subject_rel in {"target", "target_template"}:
                # linkage = { require = "static", why = "..." }: the author's
                # linkage_require(t, l, why), whose reason is mandatory.
                if not isinstance(value, dict) or set(value) - {"require", "why"}:
                    raise SpecError(
                        f"{origin}: [{subject_rel}.{subject}] linkage must be "
                        '{ require = "static" | "shared" | "both", why = "..." }'
                    )
                if "require" not in value:
                    raise SpecError(f"{origin}: [{subject_rel}.{subject}] linkage needs require")
                add_row(
                    "linkage_require",
                    [subject, value["require"], *([value["why"]] if "why" in value else [])],
                    origin,
                )
                continue
            rel = resolve_key(subject_rel, key)
            if rel not in schema.fields:
                applicable = [
                    r
                    for r, fs in sorted(schema.fields.items())
                    if fs and fs[0] == fields[0] and r != subject_rel
                ]
                hint = (
                    f"; applicable from {subject_rel}: {', '.join(applicable)}"
                    if applicable
                    else ""
                )
                suggestion = _suggest(key, schema.fields)
                did = f"; did you mean {suggestion}?" if suggestion else ""
                raise SpecError(f"{origin}: {key} is not a relation{did}{hint}")
            key_fields = schema.fields.get(rel, [])
            if fields and key_fields and _role(key_fields[0]) != _role(fields[0]):
                warn(
                    origin,
                    f"{key} under [{subject_rel}.{subject}] states {rel}({subject}, ...), but "
                    f"{rel}'s first column is {key_fields[0]}, not {fields[0]}: a top-level "
                    "key after a table header belongs to that table (move it above the header)",
                )
            try:
                if isinstance(value, dict) and set(value) & set(key_fields[1:]):
                    expand_named_row(rel, subject, value, origin)
                    continue
                for vals in _expand_value(rel, [subject], value, schema, origin):
                    add_row(rel, vals, origin)
            except SpecError as exc:
                raise SpecError(
                    f"{exc} (inside [{subject_rel}.{subject}], where {rel} rows start with "
                    f"{subject!r}; a top-level relation after a table header?)"
                ) from exc

    captured = data.get("vars", {})
    if isinstance(captured, dict):
        for key in captured:
            if key in schema.fields:
                line = _table_key_line(lines, f"{prefix}vars", key, table_line.get(("vars", 0), 1))
                warn(
                    f"{path}:{line}",
                    f"{key} under [vars] is a variable, not the relation {key}: a top-level "
                    "key after a table header belongs to that table (move it above [vars])",
                )
    for rel, value in data.items():
        if not is_relation_key(rel) or rel in (DEFAULTS_KEY, PROFILE_KEY):
            continue
        if (
            isinstance(value, dict)
            and rel in schema.fields
            and value
            and all(isinstance(v, dict) for v in value.values())
        ):
            for subject, table in value.items():
                # [rel.subject] as a header, else `subject = { ... }` under [rel]
                line_no = table_line.get((f"{rel}.{subject}", 0)) or _table_key_line(
                    lines, f"{prefix}{rel}", str(subject), table_line.get((rel, 0), 1)
                )
                expand_subject(rel, str(subject), table, line_no)
            continue
        if isinstance(value, dict) and "." in rel:
            subject_rel, subject = rel.split(".", 1)
            expand_subject(subject_rel, subject, value, table_line.get((rel, 0), 1))
            continue
        if rel not in schema.inputs and rel not in schema.fields:
            suggestion = _suggest(rel, schema.fields)
            extra = f"; did you mean {suggestion}?" if suggestion else ""
            raise SpecError(f"{path}: unknown fact relation {rel}{extra}")
        if rel in schema.probe_results:
            raise SpecError(f"{path}: {rel} is a probe result; only discovery supplies it")
        if rel in schema.driver_inputs:
            raise SpecError(f"{path}: {rel} is written by the driver; facts may not supply it")
        fields = schema.fields.get(rel, [])
        if isinstance(value, dict) and not set(value) <= set(fields):
            # Map form at top level: option_decl = { FANCY = "off" }, keyed by
            # the first column (a table whose keys are all field names is
            # one row by name).
            key_lines = value_lines.get(rel, [])
            line_no = key_lines[0] if key_lines else table_line.get((rel, 0), 1)
            # Under a [rel] header each key is on its own line: cite it.
            for key, sub in value.items():
                key_line = _table_key_line(lines, f"{prefix}{rel}", str(key), line_no)
                origin = f"{path}:{key_line}"
                for vals in _expand_value(rel, [], {key: sub}, schema, origin):
                    add_row(rel, vals, origin)
            continue
        items = value if isinstance(value, list) else [value]
        for index, item in enumerate(items):
            key_lines = value_lines.get(rel, [])
            if isinstance(value, list) and index + 1 < len(key_lines):
                line_no = key_lines[index + 1]
            elif key_lines:
                line_no = key_lines[0]
            else:
                line_no = table_line.get((rel, index), 1)
            origin = f"{path}:{line_no}"
            if isinstance(item, dict):
                try:
                    values = [item[field] for field in fields]
                except KeyError as exc:
                    raise SpecError(f"{path}: {rel} row missing field {exc.args[0]}") from exc
            elif isinstance(item, list | tuple) and rel == GENERATE:
                if not item:
                    raise SpecError(f"{origin}: {rel} rows start with a template name")
                values = [item[0], GEN_ARG_SEP.join(str(v) for v in item[1:])]
            elif isinstance(item, list | tuple):
                values = list(item)
            elif len(fields) == 1:
                values = [item]
            else:
                raise TypeError(f"{path}: {rel} rows must be arrays or tables")
            add_row(rel, values, origin)
    return rows, origins


# A symbol column named `cond` holds a condition's name (dep_when,
# prop_when, install_file_when, and project relations by the same
# convention); a `Cond` column holds an expression.
COND_NAME_COLUMN = "cond"


def desugar_conditions(
    rows: dict[str, set[tuple[str, ...]]], source: dict[str, list[str]], schema: ProgramSchema
) -> None:
    """Infix conditions (conditions.py), in place: a `Cond` column's infix
    text becomes ADT text, and an inline expression in a condition-name
    column (`!ENABLE_RTTI`, `a & b`) also states the anonymous condition
    `condition(text, parse(text))` with the row's origin, so `why` shows the
    row that wrote it."""
    added: dict[tuple[str, ...], list[str]] = {}
    for rel in list(rows):
        fields = schema.fields.get(rel, [])
        types = schema.types.get(rel, [])
        exprs = [i for i, t in enumerate(types) if t == "Cond"]
        names = [
            i
            for i, (f, t) in enumerate(zip(fields, types, strict=False))
            if f == COND_NAME_COLUMN and t == "symbol" and rel != "condition"
        ]
        if not exprs and not names:
            continue
        new_rows: set[tuple[str, ...]] = set()
        # Sorted: origins merged from several rows must not follow set order.
        for row in sorted(rows[rel]):
            origins = source.get(tuple_key(rel, row), [])
            where = origins[0] if origins else rel
            new = list(row)
            for i in exprs:
                if i < len(new):
                    new[i] = conditions.parse(new[i], where)
            for i in names:
                if i < len(new) and not conditions.is_name(new[i]):
                    cond = (new[i], conditions.parse(new[i], where))
                    added.setdefault(cond, []).extend(
                        o for o in origins if o not in added.get(cond, [])
                    )
            tup = tuple(new)
            new_rows.add(tup)
            if tup != row and origins:
                source.setdefault(tuple_key(rel, tup), []).extend(origins)
        rows[rel] = new_rows
    for cond, origins in added.items():
        rows.setdefault("condition", set()).add(cond)
        dest = source.setdefault(tuple_key("condition", cond), [])
        dest.extend(o for o in origins if o not in dest)


# Generators (UX pass T3; layers/core/V_generators.dl): the loader parses
# argv templates into gen_word/gen_file/gen_primary rows and expands
# templates (generator_params) per generate row.
GENERATE = "generate"
GEN_ARG_SEP = "\u241f"
GEN_RELS = (
    "generator",
    "generator_owner",
    "generator_label",
    "generator_run",
    "generator_output",
    "generator_input",
    "generator_stdout",
    "generator_stdin",
)
PLACEHOLDER_RE = re.compile(r"\$\{(out|src|pkg|gen|tool|list)(?::([^}]*))?\}")
GEN_RANK_STRIDE = 1000


def _gen_word(word: str, where: str) -> tuple[str, str, str, str]:
    """(prefix, kind, value, rel) of one template word."""
    found = list(PLACEHOLDER_RE.finditer(word))
    if not found:
        return "", "lit", word, ""
    if len(found) > 1:
        raise SpecError(f"{where}: generator word {word!r} has more than one placeholder")
    m = found[0]
    if m.end() != len(word):
        raise SpecError(
            f"{where}: generator word {word!r}: text after a placeholder is not supported"
        )
    kind, value = m.group(1), m.group(2) or ""
    prefix = word[: m.start()]
    if kind != "gen" and not value:
        raise SpecError(f"{where}: generator word {word!r}: ${{{kind}:...}} needs a value")
    if kind == "list" and prefix:
        raise SpecError(f"{where}: generator word {word!r}: a spliced list takes no prefix")
    if kind == "pkg":
        target, slash, rel = value.partition("/")
        return (prefix, "pkg", target, rel) if slash else (prefix, "pkgkey", value, "")
    return prefix, kind, value, ""


def desugar_generators(
    rows: dict[str, set[tuple[str, ...]]], source: dict[str, list[str]], schema: ProgramSchema
) -> None:
    """In place: one generator per generate row of a template (a generator
    with params; its text's {param} holes filled), then every generator's
    words parsed into gen_word (argv), gen_file (other files) and
    gen_primary (its first output, the action's id). Each row's origins are
    those of the rows it came from, template and instance."""
    if "gen_word" not in schema.fields:
        return

    def origins_of(rel: str, row: tuple[str, ...]) -> list[str]:
        return source.get(tuple_key(rel, row), [])

    def add(rel: str, row: tuple[str, ...], origins: list[str]) -> None:
        rows.setdefault(rel, set()).add(row)
        dest = source.setdefault(tuple_key(rel, row), [])
        dest.extend(o for o in origins if o not in dest)

    params: dict[str, list[tuple[int, str]]] = {}
    for g, pos, name in rows.get("generator_params", set()):
        params.setdefault(g, []).append((int(pos), name))
    for tpl, args in sorted(rows.get(GENERATE, set())):
        inst_origins = origins_of(GENERATE, (tpl, args))
        where = inst_origins[0] if inst_origins else GENERATE
        if tpl not in params:
            raise SpecError(f"{where}: generate: {tpl} is not a generator template (no params)")
        names = [n for _, n in sorted(params[tpl])]
        values = args.split(GEN_ARG_SEP) if args else []
        if len(values) != len(names):
            raise SpecError(
                f"{where}: generate {tpl} takes {len(names)} values ({', '.join(names)}), "
                f"this row has {len(values)}"
            )
        mapping = dict(zip(names, values, strict=True))
        hole = re.compile(r"\{(" + "|".join(re.escape(n) for n in names) + r")\}")
        inst = f"{tpl}({', '.join(values)})"
        for rel in GEN_RELS:
            for row in sorted(rows.get(rel, set())):
                if row[0] != tpl:
                    continue
                new = (inst, *(hole.sub(lambda m, mp=mapping: mp[m.group(1)], v) for v in row[1:]))
                add(rel, new, origins_of(rel, row) + inst_origins)
    for rel in (*GEN_RELS, "generator_params"):
        if rel in rows:
            rows[rel] = {row for row in rows[rel] if row[0] not in params}

    outs: dict[str, list[tuple[int, str, list[str]]]] = {}
    for g, pos, word in rows.get("generator_run", set()):
        origins = origins_of("generator_run", (g, pos, word))
        where = origins[0] if origins else f"generator {g}"
        prefix, kind, value, rel = _gen_word(word, where)
        if int(pos) == 0 and kind == "lit":
            kind = "tool"
        rank = int(pos) * GEN_RANK_STRIDE
        add("gen_word", (g, str(rank), prefix, kind, value, rel), origins)
        if kind == "out":
            outs.setdefault(g, []).append((rank, value, origins))
    roles = {
        "generator_output": ("out", {"out"}),
        "generator_input": ("in", {"src", "pkg", "pkgkey"}),
        "generator_stdout": ("stdout", {"out"}),
        "generator_stdin": ("stdin", {"src", "pkg", "pkgkey"}),
    }
    for src_rel, (role, kinds) in roles.items():
        for row in rows.get(src_rel, set()):
            g, word = row[0], row[-1]
            origins = origins_of(src_rel, row)
            where = origins[0] if origins else f"generator {g}"
            prefix, kind, value, rel = _gen_word(word, where)
            if kind == "lit" and role == "out":
                kind, value = "out", word
            if kind not in kinds or prefix:
                raise SpecError(
                    f"{where}: generator {role} {word!r} must be a "
                    + " or ".join(f"${{{k}:...}}" for k in sorted(kinds))
                )
            add("gen_file", (g, role, kind, value, rel), origins)
            if kind == "out":
                order = int(row[1]) if len(row) == 3 else 0
                base = (1 if role == "out" else 2) * GEN_RANK_STRIDE * GEN_RANK_STRIDE
                outs.setdefault(g, []).append((base + order, value, origins))
    for g, found in outs.items():
        _, first, origins = min(found)
        add("gen_primary", (g, first), origins)


def desugar_export_rules(
    rows: dict[str, set[tuple[str, ...]]],
    source: dict[str, list[str]],
    schema: ProgramSchema,
    source_root: Path,
) -> None:
    """In place: export_rules(pkg, dir), the project's own rule package that
    its install ships, as one export_rules_file(pkg, rel, src) per file the
    package's manifest lists (manifest.toml, layers, facts)."""
    if "export_rules_file" not in schema.inputs:
        return
    for row in sorted(rows.get("export_rules", set())):
        pkg, directory = row
        origins = source.get(tuple_key("export_rules", row), [])
        where = origins[0] if origins else "export_rules"
        if not directory.startswith("src:"):
            raise SpecError(f"{where}: export_rules {directory!r} must be a src: directory")
        base = source_root / directory[len("src:") :]
        manifest = base / "manifest.toml"
        if not manifest.is_file():
            raise SpecError(
                f"{where}: export_rules {directory}: no manifest.toml (not a rule package)"
            )
        data = read_toml(manifest)
        if data.get("name") != pkg:
            raise SpecError(
                f"{where}: export_rules {directory} is the package {data.get('name')!r}; "
                f"it ships as {pkg!r}, so it must be named {pkg!r}"
            )
        files = ["manifest.toml", *data.get("layers", []), *data.get("facts", [])]
        for rel in dict.fromkeys(files):
            if rel.startswith("/") or ".." in Path(rel).parts:
                raise SpecError(f"{where}: {manifest} lists {rel!r}, outside the package")
            if not (base / rel).is_file():
                raise SpecError(f"{where}: {manifest} lists missing file {rel}")
            new = (pkg, rel, f"{directory.rstrip('/')}/{rel}")
            rows.setdefault("export_rules_file", set()).add(new)
            source.setdefault(tuple_key("export_rules_file", new), list(origins))


def project_source_root(project: Path, overrides: dict[str, str] | None = None) -> Path:
    """Where a project's `src:` paths are rooted: buildlog.toml's source_root
    (with its vars), else the project's `project/` directory if it has one,
    else the project directory."""
    root = project / "project" if (project / "project").exists() else project
    config = project_config_file(project)
    if config is not None:
        data = read_project_config(config)
        if "source_root" in data:
            root = Path(
                substitute_vars(
                    str(data["source_root"]), default_vars(project, overrides), f"{config}:1"
                )
            ).expanduser()
            if not root.is_absolute():
                root = project / root
    return root


# ---- source projects (a dependency built in-tree from its spec) -------------
# L3_discovery.dl's source_project(name, path), a user relation: the driver
# loads the project at `path` into this build. Only what defines targets
# and their usage requirements is kept; the source project's build policy
# (configurations, requests), its tests and its install/export are the
# level above's and are dropped. Everything else is not supported yet (it
# is rejected, never ignored): compiled sources, configured files and rules
# are rooted in the source project, which the single source root of the
# emitted graph cannot express yet.
SOURCE_PROJECT_KEEP = {
    "target",
    "target_private",
    "target_public",
    "target_interface",
    "target_when",
    "target_deps",
    "target_deps_when",
    "dep",
    "dep_when",
    "prop",
    "prop_when",
    "condition",
    "option",
    "option_default",
    "option_help",
    "option_type",
    "option_choice",
    "option_requires",
    "option_conflicts",
    "option_decl",
}
SOURCE_PROJECT_DROP = {
    "config_decl",
    "config_decl_setting",
    "config_default",
    "request",
    "request_all",
    "test",
    "test_arg",
    "test_arg_ref",
    "test_env",
    "test_label",
    "test_regex",
    "test_serial",
    "test_when",
    "install_map",
    "install_file",
    "install_file_when",
    "install_target",
    "install_configured",
    "target_install_when",
    "export",
    "export_package",
    "export_version",
    "export_compat",
    "export_description",
}
# A source project's components keep their package names (P:comp, P):
# export_component becomes package_component (L3_install.dl).
SOURCE_PROJECT_RENAME = {"export_component": "package_component"}


def source_projects(
    user_files: list[Path] | None, variables: dict[str, str]
) -> list[tuple[str, Path, str]]:
    """(name, project directory, origin) of every source_project row of the
    user files. A relative path is relative to the user file."""
    out: list[tuple[str, Path, str]] = []
    for path in user_files or []:
        rows: list[tuple[tuple[str, ...], str]] = []
        path, table = user_file_parts(path)
        if (path.suffix == ".toml" or table) and path.is_file():
            data = read_toml(path)
            for part in table:
                data = data.get(part, {})
            value = data.get("source_project")
            if value is None:
                continue
            if isinstance(value, dict):
                items = [
                    [k, v.get("path") if isinstance(v, dict) else None] for k, v in value.items()
                ]
            elif isinstance(value, list):
                items = value
            else:
                items = [value]
            for item in items:
                if (
                    not isinstance(item, list)
                    or len(item) != 2
                    or not all(isinstance(x, str) for x in item)
                ):
                    raise SpecError(f"{path}: source_project rows are [name, path]")
                row = tuple(substitute_vars(x, variables, str(path)) for x in item)
                rows.append((row, str(path)))
        elif path.suffix == ".facts" and path.stem == "source_project":
            rows += [(r, str(path)) for r in read_rows(path)]
        elif path.suffix == ".dl" and path.is_file():
            loaded, origins = project_dl_fact_rows([path])
            for r in loaded.get("source_project", set()):
                rows.append((r, origins[tuple_key("source_project", r)][0]))
        for row, origin in rows:
            if len(row) != 2:
                raise SpecError(f"{origin}: source_project rows are [name, path]")
            directory = Path(row[1]).expanduser()
            if not directory.is_absolute():
                directory = path.parent / directory
            out.append((row[0], directory.resolve(), origin))
    return sorted(set(out))


def load_source_project(
    name: str, directory: Path, schema: ProgramSchema, origin: str, uses: list[str]
) -> tuple[dict[str, set[tuple[str, ...]]], dict[str, list[str]]]:
    """The kept rows of a source project's spec, with its `src:` paths made
    absolute under its own source root, and their origins."""
    where = f"{origin}: source project {name} ({directory})"
    config_file = project_config_file(directory)
    if config_file is None:
        raise SpecError(f"{where}: no buildlog.toml (or pyproject.toml [tool.buildlog])")
    spec = project_spec(directory)
    if spec.dls:
        raise SpecError(f"{where}: .dl spec files are not supported in a source project yet")
    config = read_project_config(config_file)
    missing = sorted(set(config.get("uses", [])) - set(uses))
    if missing:
        raise SpecError(f"{where}: uses {', '.join(missing)}, which this project does not load")
    variables = default_vars(directory)
    root = project_source_root(directory)
    rows: dict[str, set[tuple[str, ...]]] = {}
    source: dict[str, list[str]] = {}
    loaded: list[tuple[dict[str, set[tuple[str, ...]]], dict[str, list[str]]]] = []
    for path in spec.facts:
        rel = path.stem
        loaded.append(
            (
                {rel: set(read_rows(path))},
                {
                    tuple_key(rel, tuple(ln.split("\t"))): [f"{path}:{no}"]
                    for no, ln in enumerate(path.read_text().splitlines(), 1)
                    if ln.strip()
                },
            )
        )
    profiles = collect_profiles(spec.tomls)
    for path in spec.tomls:
        loaded.append(load_toml_file(path, schema, variables, profiles))

    def rebase(value: str, rel: str) -> str:
        if value.startswith("src:"):
            return os.path.normpath(root / value[4:]) if value[4:] else str(root)
        if value.startswith("out:"):
            raise SpecError(f"{where}: {rel} names a build output ({value}); not supported yet")
        return value

    for got, origins in loaded:
        for rel, vals in got.items():
            if rel in SOURCE_PROJECT_DROP or not vals:
                continue
            if rel in SOURCE_PROJECT_RENAME:
                for row in vals:
                    new_rel = SOURCE_PROJECT_RENAME[rel]
                    rows.setdefault(new_rel, set()).add(row)
                    source.setdefault(tuple_key(new_rel, row), []).extend(
                        origins.get(tuple_key(rel, row), [])
                    )
                continue
            if rel not in SOURCE_PROJECT_KEEP:
                first = origins.get(tuple_key(rel, min(vals)), [where])[0]
                raise SpecError(f"{first}: {rel} is not supported in a source project yet ({name})")
            for row in vals:
                new = tuple(rebase(v, rel) for v in row)
                rows.setdefault(rel, set()).add(new)
                source.setdefault(tuple_key(rel, new), []).extend(
                    origins.get(tuple_key(rel, row), [])
                )
    desugar_conditions(rows, source, schema)
    return namespace_source_project(name, rows, source)


OPTION_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
OPTION_REF_RE = re.compile(r"(?<![\w.])option\.([A-Za-z_]\w*)(?![\w.])")
NAMED_REF_RE = re.compile(r"\$?Named\(\s*([^\s)]+)\s*\)")


def namespace_source_project(
    name: str, rows: dict[str, set[tuple[str, ...]]], source: dict[str, list[str]]
) -> tuple[dict[str, set[tuple[str, ...]]], dict[str, list[str]]]:
    """A source project's options and conditions are its own: option X
    becomes `name.X` (the user sets it as such, with `option_value` or
    -D) and a condition c it defines becomes `name.c`, with the references
    in its conditions, dep_when and prop_when rewritten to match. Target
    names are not namespaced."""
    options = {r[0] for r in rows.get("option_decl", set())} | {
        r[0] for r in rows.get("option", set())
    }
    conds = {r[0] for r in rows.get("condition", set())}

    def expr(e: str) -> str:
        e = OPTION_REF_RE.sub(lambda m: f"option.{name}.{m[1]}" if m[1] in options else m[0], e)
        return NAMED_REF_RE.sub(
            lambda m: f"$Named({name}.{m[1]})" if m[1] in options | conds else m[0], e
        )

    def infix_expr(e: str) -> str:
        for option in sorted(options, key=len, reverse=True):
            e = re.sub(rf"(?<![\w.]){re.escape(option)}(?![\w.])", f"{name}.{option}", e)
        return e

    def cond(c: str) -> str:
        if c in options:
            return f"{name}.{c}"
        if c in conds and conditions.is_name(c):
            return f"{name}.{c}"
        return c if conditions.is_name(c) else infix_expr(c)

    rename = {
        "option": lambda r: (f"{name}.{r[0]}", *r[1:]),
        "option_default": lambda r: (f"{name}.{r[0]}", *r[1:]),
        "option_help": lambda r: (f"{name}.{r[0]}", *r[1:]),
        "option_type": lambda r: (f"{name}.{r[0]}", *r[1:]),
        "option_choice": lambda r: (f"{name}.{r[0]}", *r[1:]),
        "option_requires": lambda r: (f"{name}.{r[0]}", expr(r[1]), *r[2:]),
        "option_conflicts": lambda r: (f"{name}.{r[0]}", f"{name}.{r[1]}", *r[2:]),
        "option_decl": lambda r: (f"{name}.{r[0]}", *r[1:]),
        "condition": lambda r: (cond(r[0]), expr(r[1])),
        "dep_when": lambda r: (*r[:3], cond(r[3]), *r[4:]),
        "prop_when": lambda r: (*r[:4], cond(r[4]), *r[5:]),
        "target_when": lambda r: (r[0], cond(r[1]), *r[2:]),
        "target_deps_when": lambda r: (r[0], cond(r[1]), *r[2:]),
    }
    out_rows: dict[str, set[tuple[str, ...]]] = {}
    out_source: dict[str, list[str]] = {}
    for rel, vals in rows.items():
        fn = rename.get(rel)
        for row in vals:
            new = fn(row) if fn else row
            out_rows.setdefault(rel, set()).add(new)
            out_source.setdefault(tuple_key(rel, new), []).extend(
                source.get(tuple_key(rel, row), [])
            )
    return out_rows, out_source


def collect_project_facts(
    project: Path,
    schema: ProgramSchema,
    out: Path,
    package_facts: list[Path] | None = None,
    user_files: list[Path] | None = None,
    var_overrides: dict[str, str] | None = None,
    shuffle_seed: int | None = None,
) -> tuple[dict[str, list[str]], dict[tuple[str, str], str]]:
    """Write every input relation's rows: the project spec plus package facts files."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    rows, source_map, author_decisions = project_fact_rows(
        project,
        schema,
        package_facts=package_facts,
        user_files=user_files,
        var_overrides=var_overrides,
    )
    for rel in sorted(schema.inputs):
        write_rows(out / f"{rel}.facts", rows.get(rel, set()), shuffle_seed)
    (out / "source_map.json").write_text(json.dumps(source_map, indent=2, sort_keys=True) + "\n")
    return source_map, author_decisions


def project_fact_rows(
    project: Path,
    schema: ProgramSchema,
    package_facts: list[Path] | None = None,
    user_files: list[Path] | None = None,
    var_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, set[tuple[str, ...]]], dict[str, list[str]], dict[tuple[str, str], str]]:
    """Every input relation's rows from the project spec, the user's inputs
    and package facts files, with their source map and author decisions."""
    rows = {rel: set() for rel in schema.inputs}
    author_schema = (
        schema
        if schema.author_relations is None
        else replace(
            schema,
            fields={r: v for r, v in schema.fields.items() if r in schema.author_relations},
            types={r: v for r, v in schema.types.items() if r in schema.author_relations},
            inputs=schema.inputs & (schema.author_inputs or set()),
        )
    )
    source_map: dict[str, list[str]] = {}
    variables = default_vars(project, var_overrides)
    spec = project_spec(project)
    facts, tomls = spec.facts.copy(), spec.tomls.copy()
    author_decisions: dict[tuple[str, str], str] = {}

    def user_key(rel: str, row: tuple[str, ...]) -> str:
        if rel == "toolchain_pin" and len(row) >= 3:
            return f"{row[0]} running on {row[1]} for {row[2]}"
        if rel in {"package_pin", "package_pin_on"} and row:
            return " | ".join(row[:-1])
        if rel in {"package_linkage", "package_linkage_on"} and row:
            return " | ".join(row[:-1])
        if rel == "exec_setting" and row:
            return row[0]
        if rel == "config_default" and row:
            return row[0]
        if rel in {"alias", "option_value"} and row:
            return row[0]
        return " | ".join(row)

    def add_loaded(
        loaded_rows: dict[str, set[tuple[str, ...]]],
        source: dict[str, list[str]],
        *,
        kind: str,
        path: Path,
        module: ModuleFacts | None = None,
    ) -> None:
        input_schema = schema if kind == "package" else author_schema
        desugar_conditions(loaded_rows, source, input_schema)
        for rel, vals in loaded_rows.items():
            if (
                schema.author_relations is not None
                and kind != "package"
                and vals
                and rel not in input_schema.inputs
            ):
                origin = source.get(tuple_key(rel, min(vals)), [str(path)])[0]
                raise SpecError(f"{origin}: relation {rel} is not an input of the explicit schema")
            if rel == "option_decl" and kind in {"author", "package"}:
                for row in sorted(vals):
                    if row and not OPTION_NAME_RE.fullmatch(row[0]):
                        origin = source.get(tuple_key(rel, row), [str(path)])[0]
                        raise SpecError(
                            f"{origin}: option name {row[0]!r} is not an identifier "
                            "(a source project's options are qualified by the driver)"
                        )
            if rel == "option_value" and kind in {"author", "package"}:
                for row in sorted(vals):
                    if row and "." in row[0]:
                        origin = source.get(tuple_key(rel, row), [str(path)])[0]
                        raise SpecError(
                            f"{origin}: {row[0]} is a source project's option; only the "
                            "user sets it (option_value in a --user file), and [author_decides] "
                            "does not apply"
                        )
            if rel in schema.user_inputs and kind in {"author", "package"}:
                reason = spec.author_decides.get(rel)
                for row in sorted(vals):
                    origins = source.get(tuple_key(rel, row), [str(path)])
                    origin = origins[0]
                    if kind == "package":
                        raise SpecError(
                            f"{origin}: {rel} is a user relation; package facts may not supply it"
                        )
                    if not reason:
                        raise SpecError(
                            f"{origin}: {rel} is a user relation. Move this fact to a file "
                            "passed with --user, or add [author_decides] "
                            f'{rel} = "reason" to buildlog.toml for an auditable bypass.'
                        )
                    author_decisions[(rel, user_key(rel, row))] = reason
            if rel in schema.inputs:
                destination = module.inputs[rel] if module is not None else rel
                rows.setdefault(destination, set()).update(vals)
            else:
                raise SpecError(f"{path}: relation {rel} is not an input")
        for key, value in source.items():
            if module is not None:
                relation, row = json.loads(key)
                key = tuple_key(module.inputs[relation], tuple(row))
            source_map.setdefault(key, []).extend(value)

    module_data = {}

    def package_rows(path, loaded, source):
        module = schema.module_facts.get(path.resolve())
        if module is None:
            add_loaded(loaded, source, kind="package", path=path)
            return
        for relation, values in loaded.items():
            if values and relation not in module.allowed:
                raise SpecError(
                    f"{path}: package facts supply {relation} without declaring accepts or extends"
                )
        values, origins, _, _ = module_data.setdefault(module.module, ({}, {}, module, path))
        for relation, entries in loaded.items():
            values.setdefault(relation, set()).update(entries)
        for key, entries in source.items():
            origins.setdefault(key, []).extend(entries)

    for path in package_facts or []:
        if path.suffix == ".facts":
            rel = path.stem
            loaded = {rel: set(read_rows(path))}
            source = {
                tuple_key(rel, tuple(line.rstrip("\n").split("\t"))): [f"{path}:{no}"]
                for no, line in enumerate(path.read_text().splitlines(), 1)
                if line.strip()
            }
            package_rows(path, loaded, source)
        else:
            loaded_rows, source = load_toml_file(path, schema, package_vars(path, variables))
            package_rows(path, loaded_rows, source)
    for loaded, source, module, path in module_data.values():
        desugar_conditions(loaded, source, schema)
        desugar_generators(loaded, source, schema)
        desugar_export_rules(loaded, source, schema, project_source_root(project, var_overrides))
        add_loaded(loaded, source, kind="package", path=path, module=module)
    for path in facts:
        rel = path.stem
        if rel not in author_schema.inputs:
            raise SpecError(f"{path}: no .input declaration for relation {rel}")
        if rel in schema.probe_results:
            raise SpecError(f"{path}: {rel} is a probe result; only discovery supplies it")
        if rel in schema.driver_inputs:
            raise SpecError(f"{path}: {rel} is written by the driver; facts may not supply it")
        loaded = {rel: set()}
        source: dict[str, list[str]] = {}
        for no, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip():
                row = tuple(line.rstrip("\n").split("\t"))
                loaded[rel].add(row)
                source.setdefault(tuple_key(rel, row), []).append(f"{path}:{no}")
        add_loaded(loaded, source, kind="author", path=path)
    profiles = collect_profiles(tomls)
    for path in tomls:
        loaded_rows, source = load_toml_file(path, author_schema, variables, profiles)
        add_loaded(loaded_rows, source, kind="author", path=path)
    dl_rows, dl_source = project_dl_fact_rows(spec.dls)
    for rel in dl_rows:
        if rel not in author_schema.inputs:
            raise SpecError(
                f"{', '.join(str(p) for p in spec.dls)}: no .input declaration for relation {rel}"
            )
    add_loaded(dl_rows, dl_source, kind="author", path=spec.dls[0] if spec.dls else spec.config)
    for path in user_files or []:
        path, table = user_file_parts(path)
        if not path.is_file():
            raise BuildlogError(f"--user {path}: file does not exist")
        if table:
            loaded_rows, source = load_toml_file(path, author_schema, variables, within=table)
        elif path.suffix == ".toml":
            loaded_rows, source = load_toml_file(path, author_schema, variables)
        elif path.suffix == ".facts":
            rel = path.stem
            loaded_rows = {rel: set(read_rows(path))}
            source = {
                tuple_key(rel, tuple(line.rstrip("\n").split("\t"))): [f"{path}:{no}"]
                for no, line in enumerate(path.read_text().splitlines(), 1)
                if line.strip()
            }
        elif path.suffix == ".dl":
            loaded_rows, source = project_dl_fact_rows([path])
        else:
            raise BuildlogError(f"--user {path}: must end in .toml, .facts or .dl")
        add_loaded(loaded_rows, source, kind="user", path=path)
    for key, value in sorted((var_overrides or {}).items()):
        if not key.startswith(OPTION_PREFIX):
            continue
        # -O NAME=VALUE: the user's value, over any option_value a --user
        # file states.
        name = key[len(OPTION_PREFIX) :]
        rows["option_value"] = {r for r in rows.get("option_value", set()) if r[0] != name}
        row = (name, value)
        add_loaded(
            {"option_value": {row}},
            {tuple_key("option_value", row): [f"-O {name}={value}"]},
            kind="user",
            path=spec.config,
        )
    uses = read_toml(spec.config).get("uses", [])
    for name, directory, origin in source_projects(user_files, variables):
        loaded_rows, source = load_source_project(name, directory, author_schema, origin, uses)
        add_loaded(loaded_rows, source, kind="source", path=directory)
    # Each [author_decides] entry also holds relation-wide (key "*"), for
    # rules that check author facts outside the user relation itself (e.g.
    # buildlog-cpp's -Werror in a compile flag, bypassed by cc_werror).
    for rel, reason in spec.author_decides.items():
        author_decisions[(rel, "*")] = reason
    desugar_generators(rows, source_map, schema)
    desugar_export_rules(rows, source_map, schema, project_source_root(project, var_overrides))
    for relation, marker in (
        ("target", "source_target_input"),
        ("imported", "source_imported_input"),
        ("alias", "source_alias_input"),
    ):
        if marker in schema.inputs:
            rows[marker] = set(rows.get(relation, set()))
            for row in rows[marker]:
                source_map[tuple_key(marker, row)] = list(
                    source_map.get(tuple_key(relation, row), [])
                )
    return rows, source_map, author_decisions
