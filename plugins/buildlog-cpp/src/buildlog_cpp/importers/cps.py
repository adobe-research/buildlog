# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Common Package Specification (`.cps`) importer.

Follows https://cps-org.github.io/cps/ (schema, configurations, searching).
Targets are `<Pkg>:<component>`, the CPS spelling of a component, and
`<Pkg>:*`, an interface target over the package's `default_components` (what
a consumer gets who names the package but no component). The file states
the package's name, and a CPS requirement naming only the package means its
default components, so the import states `alias("Pkg", "Pkg:*")` (when the
package has `default_components`); a search for the bare name `Pkg` looks
for that package. CPS states no other name equivalences; in particular
`Pkg:comp` is not CMake's `Pkg::comp`, even where CMake generated both
files. A package named `pc` is rejected (`pc:<module>` ids are
pkg-config's).

Supported:
- package: `name`, `cps_version`, `components`, `default_components`,
  `configurations` (only as the list of names; there is no preference
  fallback, see below), `prefix`, `cps_path` (prefix determination against
  the file's directory, its realpath, and the realpath's directory),
  `platform` (`isa` and `kernel`), and the tool extension `x_buildlog_rules`
  (rule package paths, relative to the `.cps` file, fed to the outer loop).
- configuration merging: `<name>:*.cps`, `<name>-*.cps` (common specs whose
  components are appended) and `<name>@*.cps`, `<name>:*@*.cps`,
  `<name>-*@*.cps` (configuration-specific specs: only `name`,
  `configuration` and `components`; their attributes belong to the
  component-configuration).
- component types `archive`, `dylib` (libraries), `module`, `executable`,
  `interface` and `symbolic`; unknown types are ignored, per the schema.
- component attributes `location`, `requires` (public edges; `:c` is this
  package's component), `link_requires` (private edges), `includes`,
  `definitions`, `compile_flags` (language maps: `*` and `cpp` apply, other
  languages apply to no buildlog compilation), `link_flags`,
  `link_libraries`, and `compile_features` (`c++NN` is `cxx.std`, `threads`
  is `-pthread`).
- per-configuration attributes, which override the component's value
  exactly as the spec says (a `null` unsets it). Configuration names map to
  buildlog settings through `common.CONFIG_SETTINGS`; an unmapped
  configuration never applies.

Rejected (a target error, reported only if the target is needed):
`link_location` (import libraries), `cpp_module_metadata`,
`compile_features` other than the above, and `!NAME` definitions. Rejected
for the whole file: malformed JSON, a wrong-typed attribute, an unsupported
`platform` attribute, `@prefix@` without `prefix` or a matching `cps_path`.
Ignored, per the schema: unknown attributes and the descriptive ones.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from buildlog.layers import parse_manifest

from . import jsonlines
from .common import (
    CPS_DEFAULT,
    FALSE,
    PC_PREFIX,
    TRUE,
    Cond,
    MetadataError,
    Result,
    Vocabulary,
    and_,
    cps_id,
    cpu_domain,
    eq,
    exact,
    not_,
    or_,
    os_cond,
    os_domain,
    package_config_cond,
    stat_token,
)

KINDS = {
    "archive": "library",
    "dylib": "library",
    "module": "module",
    "executable": "executable",
    "interface": "interface",
    "symbolic": "interface",
}
# Languages whose attributes apply to buildlog's C++ compilations.
LANGS = ("*", "cpp")
CXX_STDS = {"11", "14", "17", "20", "23", "26"}
KERNELS = {"darwin": "macos", "macos": "macos", "linux": "linux", "windows": "windows"}
ISAS = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}
CONFIG_SPEC_KEYS = {"name", "configuration", "components"}
ATTRS = (
    "location",
    "link_location",
    "requires",
    "link_requires",
    "includes",
    "definitions",
    "compile_flags",
    "link_flags",
    "link_libraries",
    "compile_features",
    "cpp_module_metadata",
)
SUPPLEMENTS = ("{n}:*.cps", "{n}-*.cps", "{n}@*.cps", "{n}:*@*.cps", "{n}-*@*.cps")


def split_name(name: str) -> tuple[str, str] | None:
    """(package, component) of a CPS id `Pkg:comp` or `Pkg:*`; a bare
    package name is its default components, `Pkg:*`."""
    if "/" in name or name.startswith(PC_PREFIX):
        return None
    if ":" not in name:
        return (name, CPS_DEFAULT) if name else None
    if name.count(":") != 1:
        return None
    pkg, _, comp = name.partition(":")
    return (pkg, comp) if pkg and comp else None


def _candidate_paths(pkg: str, directory: Path) -> list[Path]:
    out: list[Path] = []
    for n in dict.fromkeys((pkg, pkg.lower())):
        out += [directory / f"{n}.cps", directory / n / f"{n}.cps"]
        for sub in sorted(directory.glob(f"{n}/*/")):
            out.append(sub / f"{n}.cps")
    return out


def by_alias(name: str) -> bool:
    """A bare package name is the alias of `<Pkg>:*`."""
    return ":" not in name


def find(name: str, directory: Path) -> list[Path]:
    parts = split_name(name)
    if parts is None:
        return []
    pkg, comp = parts
    bare = ":" not in name
    found: list[Path] = []
    for p in _candidate_paths(pkg, directory):
        if p in found or not p.is_file() or not exact(p) or not exact(p.parent):
            continue
        if bare and (p.stem != pkg or not _has_default_components(p)):
            continue  # the alias Pkg -> Pkg:* names exactly this package
        if comp == CPS_DEFAULT or comp in _component_names(p):
            found.append(p)
    return found


def offers(file: Path, directory: Path, names, observations) -> set[str]:
    if not exact(file) or not exact(file.parent):
        return set()
    components = _component_names(file)
    default = _has_default_components(file)
    candidates = {*names, file.stem, f"{file.stem}:*"}
    candidates.update(f"{file.stem}:{component}" for component in components)
    paths = {}
    found = set()
    for name in candidates:
        parts = split_name(name)
        if parts is None:
            continue
        package, component = parts
        if package not in paths:
            paths[package] = _candidate_paths(package, directory)
        if file not in paths[package]:
            continue
        if ":" not in name and (file.stem != package or not default):
            continue
        if component == CPS_DEFAULT or component in components:
            found.add(name)
    return found


def enumerate_files(directory: Path, observations) -> list[Path]:
    files = []
    level = [directory]
    for _ in range(3):
        children = [p for d in level for p in observations.directory(d)]
        files += [p for p in children if p.suffix == ".cps" and p.is_file()]
        level = [p for p in children if p.is_dir()]
    return sorted(set(files))


def metadata_links(file: Path, observations):
    from buildlog.providers import MetadataLinks

    observations.directory(file.parent)
    rules = []
    errors = []
    for path in [file, *_supplements(file)]:
        content = observations.file(path)
        if content is None:
            errors.append(f"{path}: missing metadata")
            continue
        try:
            doc = jsonlines.loads(content.decode())
            if not isinstance(doc, dict):
                raise MetadataError(1, "a CPS file is a JSON object")
            pk = Package(Result("", path), path, doc)
            if MARKER in doc:
                marker, _ = _marker_path(doc, pk)
                observations.file(Path(marker))
            if "x_buildlog_rules" in doc:
                rules += [
                    Path(pk.path_of(value, line))
                    for value, line in _strings(doc, "x_buildlog_rules")
                ]
        except (MetadataError, UnicodeError, jsonlines.JsonError) as exc:
            errors.append(f"{path}: {exc}")
    return MetadataLinks(rules=tuple(rules), error="; ".join(errors))


def _has_default_components(path: Path) -> bool:
    try:
        doc = json.loads(path.read_text())
        return isinstance(doc, dict) and "default_components" in doc
    except (OSError, ValueError):
        return False


def _component_names(path: Path) -> set[str]:
    names: set[str] = set()
    for f in [path, *_supplements(path)]:
        try:
            doc = json.loads(f.read_text())
            names |= set(doc.get("components", {}))
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return names


MARKER = "x_buildlog_manifest"


def _marker_path(doc, pk: Package) -> tuple[str, int | None]:
    """The manifest the file names as its sibling (MARKER), inside the prefix."""
    value = _want(doc, MARKER, str, "a prefix-relative path")
    line = _line(doc, MARKER)
    if pk.prefix is None:
        raise MetadataError(line, f"{MARKER} needs the package's prefix (cps_path or prefix)")
    if os.path.isabs(value) or "@" in value:
        raise MetadataError(line, f"{MARKER} must be a path relative to the prefix")
    path = os.path.normpath(os.path.join(pk.prefix, value))
    if not path.startswith(pk.prefix.rstrip("/") + "/"):
        raise MetadataError(line, f"{MARKER} {value!r} leaves the prefix")
    return path, line


def verified_marker(doc, pk: Package) -> Path | None:
    """The sibling manifest of a file that names one; an error unless it is an
    existing manifest of this package (never a silent fallback to the CPS file)."""
    if MARKER not in doc:
        return None
    path, line = _marker_path(doc, pk)
    pkg = doc.get("name")
    if not os.path.isfile(path):
        raise MetadataError(line, f"{MARKER} names no manifest: {path}")
    if Path(path).parent.name != pkg:
        raise MetadataError(
            line, f"{MARKER} names the manifest of {Path(path).parent.name!r}, not of {pkg!r}"
        )
    errors: list[str] = []
    data = parse_manifest(Path(path), errors)
    if errors or data.get("name") != pkg:
        shown = "; ".join(errors) or f"it is named {data.get('name')!r}"
        raise MetadataError(
            line, f"{MARKER}: manifest {path} is not a manifest of {pkg!r}: {shown}"
        )
    return Path(path)


def _marker_of(path: Path):
    try:
        doc = jsonlines.loads(path.read_text())
        if not isinstance(doc, dict) or MARKER not in doc:
            return None
        return doc, Package(None, path, doc)  # type: ignore[arg-type]
    except (OSError, jsonlines.JsonError, MetadataError):
        return None


def same_as(path: Path) -> list[Path]:
    """The manifest this file declares itself the same package as (core then
    sees one candidate, L3_discovery.dl). A declaration that does not hold is a
    MetadataError: the file is invalid, not a candidate."""
    try:
        doc = jsonlines.loads(path.read_text())
    except (OSError, jsonlines.JsonError):
        return []  # the import reports it
    if not isinstance(doc, dict) or MARKER not in doc:
        return []
    m = verified_marker(doc, Package(None, path, doc))  # type: ignore[arg-type]
    return [m] if m is not None else []


def find_watched(name: str, directory: Path) -> list:
    parts = split_name(name)
    if parts is None:
        return []
    out = []
    for p in _candidate_paths(parts[0], directory):
        out.append(stat_token(p))
        found = _marker_of(p) if p.is_file() else None
        if found is not None:
            try:
                out.append(stat_token(Path(_marker_path(*found)[0])))
            except MetadataError:
                pass
        out += [stat_token(s) for s in _supplements(p)] if p.is_file() else []
    for n in sorted({parts[0], parts[0].lower()}):
        d = directory / n
        out.append([str(d), sorted(os.listdir(d)) if d.is_dir() else None])
    return out


def _supplements(path: Path) -> list[Path]:
    n = path.stem
    found: set[Path] = set()
    for pattern in SUPPLEMENTS:
        found |= {p for p in path.parent.glob(pattern.format(n=n)) if p != path}
    return sorted(found)


def watched(path: Path) -> list:
    return [stat_token(path), *[stat_token(p) for p in _supplements(path)]]


# ---- import -----------------------------------------------------------------


def _load(res: Result, path: Path) -> jsonlines.LDict:
    res.current = path
    try:
        doc = jsonlines.loads(path.read_text())
    except jsonlines.JsonError as exc:
        raise MetadataError(exc.line, f"invalid JSON: {exc.message}") from exc
    if not isinstance(doc, dict):
        raise MetadataError(1, "a CPS file is a JSON object")
    return doc


def _line(obj, key) -> int | None:
    lines = getattr(obj, "lines", None)
    if isinstance(lines, dict):
        return lines.get(key, getattr(obj, "line", None))
    if isinstance(lines, list) and isinstance(key, int) and key < len(lines):
        return lines[key]
    return getattr(obj, "line", None)


def _want(obj, key, types, what: str):
    v = obj[key]
    if not isinstance(v, types):
        raise MetadataError(_line(obj, key), f"{key} must be {what}")
    return v


def _strings(obj, key) -> list[tuple[str, int | None]]:
    v = _want(obj, key, list, "a list of strings")
    out = []
    for i, item in enumerate(v):
        if not isinstance(item, str):
            raise MetadataError(_line(v, i), f"{key} must be a list of strings")
        out.append((item, _line(v, i)))
    return out


class Package:
    def __init__(self, res: Result, path: Path, doc: jsonlines.LDict):
        self.res = res
        self.path = Path(os.path.abspath(path))
        self.dir = self.path.parent
        self.prefix = self._prefix(doc)

    def _prefix(self, doc) -> str | None:
        if "prefix" in doc:
            p = _want(doc, "prefix", str, "a path")
            if "@prefix@" in p or not os.path.isabs(p):
                raise MetadataError(_line(doc, "prefix"), "prefix must be an absolute path")
            return os.path.normpath(p)
        if "cps_path" not in doc:
            return None
        cp = _want(doc, "cps_path", str, "a path")
        line = _line(doc, "cps_path")
        if not cp.startswith("@prefix@"):
            if os.path.isabs(cp):
                return None
            raise MetadataError(line, "cps_path must be absolute or start with @prefix@")
        rel = cp[len("@prefix@") :].strip("/")
        tries = [
            str(self.dir),
            os.path.realpath(self.dir),
            str(Path(os.path.realpath(self.path)).parent),
        ]
        for d in tries:
            d = os.path.normpath(d)
            if not rel:
                return d
            if d.endswith("/" + rel):
                return d[: -len(rel) - 1] or "/"
        raise MetadataError(line, f"cps_path {cp!r} does not match the file's location {self.dir}")

    def path_of(self, value: str, line: int | None) -> str:
        if "@prefix@" in value:
            if self.prefix is None:
                raise MetadataError(
                    line, "@prefix@ used, but the package gives no prefix or cps_path"
                )
            value = value.replace("@prefix@", self.prefix)
        if not os.path.isabs(value):
            value = str(self.dir / value)
        return os.path.normpath(value)


def _platform(res: Result, doc, results) -> tuple[Cond, str]:
    if "platform" not in doc:
        return TRUE, ""
    plat = _want(doc, "platform", dict, "an object")
    c: Cond = TRUE
    for key, value in plat.items():
        line = _line(plat, key)
        if not isinstance(value, str):
            raise MetadataError(line, f"platform.{key} must be a string")
        if key == "kernel":
            os_name = KERNELS.get(value.lower())
            if os_name is None:
                raise MetadataError(line, f"platform kernel {value!r} has no buildlog platform.os")
            c = and_(c, os_cond(os_name, os_domain(results or {})))
        elif key == "isa":
            cpu = ISAS.get(value.lower())
            if cpu is None:
                raise MetadataError(line, f"platform isa {value!r} has no buildlog platform.cpu")
            c = and_(c, eq("platform.cpu", cpu) if cpu in cpu_domain(results or {}) else FALSE)
        else:
            raise MetadataError(line, f"platform attribute {key!r} is not supported")
    desc = ", ".join(f"{k}={v}" for k, v in plat.items())
    return c, desc


def import_file(res: Result, results=None) -> None:
    main = _load(res, res.file)
    for key in ("name", "cps_version"):
        if key not in main:
            raise MetadataError(main.line, f"missing required attribute {key}")
    pkg = _want(main, "name", str, "a string")
    if pkg != res.file.stem:
        raise MetadataError(_line(main, "name"), f"package {pkg!r} is not named like its file")
    if f"{pkg}:" == PC_PREFIX or ":" in pkg:
        raise MetadataError(_line(main, "name"), f"package name {pkg!r} is reserved or malformed")
    base = Package(res, res.file, main)
    pcond, pdesc = _platform(res, main, results)

    # (component, doc, package, file) of every common spec; configuration
    # attributes per component from the embedded and the @ specs.
    comps: dict[str, tuple[jsonlines.LDict, Package, Path]] = {}
    extra_cfgs: dict[str, dict[str, tuple[jsonlines.LDict, Path]]] = {}
    docs = [(res.file, main, base)]
    for sup in _supplements(res.file):
        doc = _load(res, sup)
        if doc.get("name") != pkg:
            raise MetadataError(
                _line(doc, "name") or 1, f"supplement does not name package {pkg!r}"
            )
        if "@" in sup.name:
            bad = sorted(set(doc) - CONFIG_SPEC_KEYS)
            if bad:
                raise MetadataError(
                    _line(doc, bad[0]), f"attribute {bad[0]} in a configuration-specific CPS"
                )
            if "configuration" not in doc:
                raise MetadataError(doc.line, "configuration-specific CPS without configuration")
            cfg = _want(doc, "configuration", str, "a string")
            for comp, cdoc in _want(doc, "components", dict, "an object").items():
                if not isinstance(cdoc, dict):
                    raise MetadataError(_line(doc["components"], comp), "a component is an object")
                per = extra_cfgs.setdefault(comp, {})
                if cfg in per:
                    raise MetadataError(_line(doc["components"], comp), f"{comp} {cfg} given twice")
                per[cfg] = (cdoc, sup)
        else:
            docs.append(
                (
                    sup,
                    doc,
                    Package(res, sup, doc) if ("prefix" in doc or "cps_path" in doc) else base,
                )
            )

    for path, doc, pk in docs:
        res.current = path
        if "components" not in doc:
            if path == res.file:
                raise MetadataError(doc.line, "missing required attribute components")
            continue
        for comp, cdoc in _want(doc, "components", dict, "an object").items():
            if not isinstance(cdoc, dict):
                raise MetadataError(_line(doc["components"], comp), "a component is an object")
            if comp == CPS_DEFAULT or ":" in comp:
                raise MetadataError(
                    _line(doc["components"], comp), f"component name {comp!r} is not allowed"
                )
            if comp in comps:
                raise MetadataError(
                    _line(doc["components"], comp), f"component {comp} defined twice"
                )
            comps[comp] = (cdoc, pk, path)
        verified_marker(doc, pk) if pk is not None else None
        if "x_buildlog_rules" in doc:
            for value, line in _strings(doc, "x_buildlog_rules"):
                res.rule_package(pk.path_of(value, line), line)

    for comp in extra_cfgs:
        if comp not in comps:
            path = next(iter(extra_cfgs[comp].values()))[1]
            res.current = path
            raise MetadataError(None, f"configuration of unknown component {comp}")

    # The package each target belongs to, by the package's own name, and
    # its version (what an export requiring the target names it by).
    version = main.get("version") if isinstance(main.get("version"), str) else None
    vline = _line(main, "version")
    for comp, (cdoc, pk, path) in comps.items():
        if _component(
            res,
            pkg,
            comp,
            cdoc,
            pk,
            path,
            extra_cfgs.get(comp, {}),
            pcond,
            pdesc,
            symbolic=isinstance(results, Vocabulary),
        ):
            res.current = res.file
            res.package(cps_id(pkg, comp), pkg, comp, _line(main, "name"))
            if version:
                res.version_string(cps_id(pkg, comp), version, vline)

    res.current = res.file
    if "default_components" in main:
        line = _line(main, "default_components")
        default = cps_id(pkg, CPS_DEFAULT)
        res.target(default, "interface", line)
        res.package(default, pkg, "", _line(main, "name"))
        if version:
            res.version_string(default, version, vline)
        res.alias(pkg, default, line)
        for dc, dline in _strings(main, "default_components"):
            if dc not in comps:
                raise MetadataError(dline, f"default component {dc} is not a component")
            res.dep(default, cps_id(pkg, dc), "public", TRUE, dline)


def _component(res, pkg, comp, cdoc, pk, path, extra, pcond, pdesc, *, symbolic=False) -> bool:
    """Import one component; False if its type defines no target."""
    res.current = path
    t = cps_id(pkg, comp)
    line = _line(cdoc, "type") or cdoc.line
    if "type" not in cdoc:
        raise MetadataError(cdoc.line, f"component {comp} has no type")
    ctype = _want(cdoc, "type", str, "a string")
    if ctype not in KINDS:
        return False
    res.target(t, KINDS[ctype], line)
    if pcond == FALSE:
        res.target_error(t, f"package is for another platform ({pdesc})", _line(cdoc, "type"))
        return True
    if symbolic:
        res.require_platform(
            t, pcond, f"package is for another platform ({pdesc})", _line(cdoc, "type")
        )

    configs: dict[str, tuple[jsonlines.LDict, Path]] = {}
    if "configurations" in cdoc:
        for cfg, cfgdoc in _want(cdoc, "configurations", dict, "an object").items():
            if not isinstance(cfgdoc, dict):
                raise MetadataError(
                    _line(cdoc["configurations"], cfg), "a configuration is an object"
                )
            configs[cfg] = (cfgdoc, path)
    for cfg, entry in extra.items():
        if cfg in configs:
            res.current = entry[1]
            raise MetadataError(entry[0].line, f"{comp} configuration {cfg} given twice")
        configs[cfg] = entry

    for attr in ATTRS:
        having = [cfg for cfg, (d, _) in configs.items() if attr in d]
        override = or_(*[package_config_cond(cfg) for cfg in having])
        if attr in cdoc:
            res.current = path
            _attr(res, pk, pkg, t, attr, cdoc, and_(pcond, not_(override)))
        for cfg in having:
            d, f = configs[cfg]
            res.current = f
            _attr(res, pk, pkg, t, attr, d, and_(pcond, package_config_cond(cfg)))
    res.current = res.file
    return True


def _by_lang(obj, key):
    """(value, line) of the applicable languages of a list-or-language-map."""
    v = obj[key]
    if isinstance(v, list):
        return [(v, _line(obj, key))]
    if not isinstance(v, dict):
        raise MetadataError(_line(obj, key), f"{key} must be a list or a language map")
    return [(v[lang], _line(v, lang)) for lang in LANGS if lang in v]


def _dep_name(pkg: str, ref: str, line) -> str:
    """`:comp` (this package) as `Pkg:comp`, `Other:comp` as is, and a bare
    `Other` (no component) as `Other:*`, its default components."""
    if ref.count(":") > 1:
        raise MetadataError(line, f"malformed requirement {ref!r}")
    if ref.startswith(":") and len(ref) > 1:
        return cps_id(pkg, ref[1:])
    other, sep, comp = ref.partition(":")
    if f"{other}:" == PC_PREFIX:
        raise MetadataError(line, f"requirement {ref!r} names the reserved package pc")
    if sep and other and comp:
        return ref
    if not sep and ref:
        return cps_id(ref, CPS_DEFAULT)
    raise MetadataError(line, f"malformed requirement {ref!r}")


def _attr(res: Result, pk: Package, pkg: str, t: str, attr: str, obj, c: Cond) -> None:
    line = _line(obj, attr)
    value = obj[attr]
    if value is None or c == FALSE:
        return
    if attr == "location":
        path = _want(obj, attr, str, "a path")
        res.location(t, pk.path_of(path, line), c, line)
    elif attr == "link_location":
        res.target_error(t, "link_location (import libraries) is not supported", line)
    elif attr == "cpp_module_metadata":
        res.target_error(t, "cpp_module_metadata (C++ modules) is not supported", line)
    elif attr in ("requires", "link_requires"):
        vis = "public" if attr == "requires" else "private"
        for ref, rline in _strings(obj, attr):
            res.dep(t, _dep_name(pkg, ref, rline), vis, c, rline)
    elif attr in ("includes", "compile_flags"):
        for items, lline in _by_lang(obj, attr):
            if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
                raise MetadataError(lline, f"{attr} must be a list of strings")
            for i, item in enumerate(items):
                iline = _line(items, i) or lline
                if attr == "includes":
                    res.prop(t, "cc.include_dir", pk.path_of(item, iline), c, iline)
                else:
                    res.prop(t, "cc.compile_flag", item, c, iline)
    elif attr == "definitions":
        v = _want(obj, attr, dict, "a language map")
        for lang in LANGS:
            if lang not in v:
                continue
            defs = v[lang]
            if not isinstance(defs, dict):
                raise MetadataError(_line(v, lang), "definitions of a language must be an object")
            for name, dv in defs.items():
                dline = _line(defs, name)
                if name.startswith("!"):
                    res.target_error(t, f"definition {name} (undefine) is not supported", dline)
                elif dv is None:
                    res.prop(t, "cc.define", name, c, dline)
                elif isinstance(dv, str):
                    res.prop(t, "cc.define", f"{name}={dv}", c, dline)
                else:
                    raise MetadataError(dline, f"definition {name} must be a string or null")
    elif attr == "link_flags":
        for flag, fline in _strings(obj, attr):
            res.prop(t, "native.link_flag", flag, c, fline)
    elif attr == "link_libraries":
        for lib, lline in _strings(obj, attr):
            if lib.startswith("-"):
                res.prop(t, "native.link_flag", lib, c, lline)
            elif os.path.isabs(lib) or "@prefix@" in lib:
                res.link(t, pk.path_of(lib, lline), c, lline)
            elif "/" in lib:
                raise MetadataError(lline, f"link library {lib!r} is a relative path")
            else:
                res.prop(t, "native.link_flag", f"-l{lib}", c, lline)
    elif attr == "compile_features":
        for feat, fline in _strings(obj, attr):
            if feat.startswith("c++") and feat[3:] in CXX_STDS:
                res.prop(t, "cxx.std", feat[3:], c, fline)
            elif feat == "threads":
                res.prop(t, "cc.compile_flag", "-pthread", c, fline)
                res.prop(t, "native.link_flag", "-pthread", c, fline)
            else:
                res.target_error(t, f"compile feature {feat!r} has no buildlog mapping", fline)
