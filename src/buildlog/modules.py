# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Identity-keyed declaration/use guards for a fixed executable union."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .available import digest

if TYPE_CHECKING:
    from .layers import Program


@dataclass(frozen=True)
class ModuleFacts:
    module: str
    inputs: dict[str, str]
    allowed: frozenset[str]


@dataclass
class ScopedModules:
    programs: dict[Path, Path] = field(default_factory=dict)
    facts: dict[Path, ModuleFacts] = field(default_factory=dict)
    inputs: set[str] = field(default_factory=set)
    rows: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)
    identities: dict[Path, str] = field(default_factory=dict)


def identity(manifest: Path) -> str:
    return "module:" + digest(str(manifest.resolve()))


def compile_modules(program: Program, outdir: Path) -> ScopedModules | None:
    from .layers import _split_top, _without_strings, ast_dump
    from .provenance.program import _mask, _statements

    if program.explicit_manifests is None:
        return None
    core = next((layer for layer in program.layers if layer.source == "core"), None)
    if core is None or "module_bound" not in core.qualified:
        return None
    scope = ScopedModules()
    scope.identities = {
        layer.manifest.resolve(): identity(layer.manifest)
        for layer in program.layers
        if layer.source != "project"
    }
    by_name = {layer.name: layer for layer in program.layers}
    dependencies: dict[str, set[str]] = {}
    types = {name: types for layer in program.layers for name, types in layer.info.decls.items()}
    for layer in program.layers:
        for _, body, text in layer.info.clauses:
            at = _without_strings(text).find(":-")
            heads = text[:at] if at >= 0 else text.rstrip().removesuffix(".")
            for atom in _split_top(heads, ","):
                match = re.match(r"\s*([\w.]+)\s*\(", atom)
                if match is not None:
                    dependencies.setdefault(match[1], set()).update(body)
    lower = {
        core.qualified.get(name, name)
        for name in (
            "defined",
            "condition",
            "package_version",
            "package_attr",
            "imported_kind",
            "import_linkage_dep",
            "import_req",
            "import_req_on",
            "import_select",
            "import_root",
            "path_want",
        )
    }
    chains = {name: (name,) for name in lower}
    pending = sorted(lower)
    while pending:
        pending_relation = pending.pop()
        for name in sorted(dependencies.get(pending_relation, ())):
            if name not in lower:
                lower.add(name)
                chains[name] = (*chains[pending_relation], name)
                pending.append(name)
    upper = {
        core.qualified.get(name, name) for name in ("needed", "cond_holds", "holds", "module_used")
    }
    if forbidden := sorted(lower & upper):
        program.errors.extend(
            "declaration/selection stratum depends on demand or condition truth: "
            + " -> ".join(chains[name])
            for name in forbidden
        )
        return None
    guarded_inputs = {relation for layer in program.layers for relation in layer.accepts} - {
        relation
        for layer in program.layers
        for relation in (layer.user | layer.driver | {r for p in layer.probes for r in p.results})
    }
    conditional_inputs = {
        match[1]
        for layer in program.layers
        for match in re.finditer(r"\.decl\s+([\w.]+)\s*\(([^)]*)\)", layer.expanded)
        if re.search(r"\bcond\s*:\s*symbol\b", match[2])
    }

    def add(relation: str, *row: str) -> None:
        scope.rows.setdefault(relation, set()).add(row)

    def guard(module: str, relation: str) -> str:
        name = "module_bound" if relation in lower else "module_used"
        return f"{core.qualified[name]}({json.dumps(module)}, _)"

    def guarded(head: str, body: str, module: str, relation: str) -> str:
        body = body.strip().removesuffix(".").strip()
        suffix = f", ({body})" if body else ""
        return f"{head.strip()} :- {guard(module, relation)}{suffix}."

    for layer in program.layers:
        if layer.source == "project":
            continue
        manifest = layer.manifest.resolve()
        module = scope.identities[manifest]
        add("module_manifest", module, str(manifest.parent))
        for name in layer.requires:
            dependency = by_name.get(name)
            if dependency is not None:
                add("module_requires", module, scope.identities[dependency.manifest.resolve()])
        for imported in layer.imports:
            add(
                "module_import",
                module,
                imported["format"],
                os.path.abspath(manifest.parent / imported["file"]),
                imported["namespace"],
            )
        if manifest in program.explicit_manifests:
            add("module_root", module)
            continue
        fragments = []
        # Souffle's AST printer does not escape string literals for reparsing.
        source = (
            ast_dump(layer.program_files, outdir / "module-inputs", layer.name, preprocess=True)
            if layer.program_files
            else ""
        )
        for start, end, kind in _statements(_mask(source)):
            text = source[start:end]
            if kind == "directive":
                fragments.append(text)
                continue
            at = _without_strings(text).find(":-")
            heads = text[:at] if at >= 0 else text.strip().removesuffix(".")
            body = text[at + 2 :] if at >= 0 else ""
            for head in _split_top(heads, ","):
                match = re.match(r"\s*([\w.]+)\s*\((.*)\)\s*$", head, re.DOTALL)
                if match is None:
                    raise ValueError(f"{layer.manifest}: cannot scope checked head {head!r}")
                relation, arguments = match.groups()
                fragments.append(guarded(head, body, module, relation))
                if relation in {"target", "imported"}:
                    declaration = (
                        f"{core.qualified['module_declared']}({json.dumps(module)}, {arguments})"
                    )
                    fragments.append(guarded(declaration, body, module, relation))
        if layer.fact_files:
            allowed = frozenset(layer.accepts | layer.extends)
            payload_inputs = set(allowed)
            if allowed & conditional_inputs:
                payload_inputs.add("condition")
            if "generator_run" in allowed:
                payload_inputs.update(("gen_word", "gen_primary"))
            if allowed & {
                "generator_output",
                "generator_input",
                "generator_stdout",
                "generator_stdin",
            }:
                payload_inputs.update(("gen_file", "gen_primary"))
            if "export_rules" in allowed:
                payload_inputs.add("export_rules_file")
            shadow = {
                relation: f"modulefact_{digest(module)[:24]}__{relation}"
                for relation in sorted(guarded_inputs & payload_inputs)
            }
            spec = ModuleFacts(module, shadow, allowed)
            for path in layer.fact_files:
                scope.facts[path.resolve()] = spec
            for relation, name in shadow.items():
                fields = types[relation]
                declaration = ", ".join(f"a{i}:{typ}" for i, typ in enumerate(fields))
                arguments = ", ".join(f"X{i}" for i in range(len(fields)))
                fragments.append(f".decl {name}({declaration})")
                fragments.append(
                    guarded(f"{relation}({arguments})", f"{name}({arguments})", module, relation)
                )
                if relation in {"target", "imported"}:
                    head = f"{core.qualified['module_declared']}({json.dumps(module)}, {arguments})"
                    fragments.append(guarded(head, f"{name}({arguments})", module, relation))
                scope.inputs.add(name)
        destination = outdir / layer.name / "module-scoped.dl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(fragments) + "\n")
        scope.programs[manifest] = destination
    return scope
