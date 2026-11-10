# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`buildlog -E export_package`: write one export file of an installed package.

A serializer, not a deriver: L3_install.dl derives every fact the file
states (the package's components, their requirements and their exported
properties, each spelled for the format by the format plugin's facts), and
this module only writes them in the format's syntax. Every path is
relative to the package's install prefix, and each format finds the prefix
from the file's own location, so the installed tree is relocatable:
CPS's `@prefix@` with `cps_path`, pkg-config's `${pcfiledir}`, CMake's
`_IMPORT_PREFIX`.

  export_package --format FMT -o OUT --package PKG --install-path REL
                 [--version V] [--compat POLICY] [--description TEXT]
                 [--arch-independent] [--pointer-size N]
                 [--component=KIND:COMP]... [--requires=COMP:REQ]...
                 [--depends=COMP:PKG:PCOMP]... [--depends-version=PKG=MIN]...
                 [--path=COMP:FIELD=REL]... [--abspath=COMP:FIELD=PATH]...
                 [--value=COMP:FIELD=VALUE]...
                 [--location=COMP=REL] [--soname=COMP=NAME] [--link-name=COMP=NAME]
                 [--runtime-dep=COMP:MODULE:PLACE]...
                 [--rules-manifest FILE]

FMT: cps | pc | cmake-config | cmake-version | cmake-targets |
buildlog-facts | buildlog-manifest (the `buildlog` format, dl_package.py: FIELD
is buildlog's own property key, nothing is spelled; `--rules-manifest` is the
manifest of the project's own rule package, which the installed manifest
extends). REL is where
OUT is installed, relative to the prefix. FIELD is the format's own name
for a property (CPS `includes`, CMake `INTERFACE_COMPILE_DEFINITIONS`,
pkg-config `define`), from the plugin's export_spelling facts.
`--requires` is a requirement on another component of this package;
`--depends` one on component PCOMP of another installed package PKG (an
empty PCOMP is the package as a whole), with `--depends-version` its
minimum version: CPS `requires` (component and package level),
pkg-config `Requires: PKG >= MIN`, CMake `find_dependency(PKG MIN)` and a
link to `PKG::PCOMP` (`PKG::PKG` for the whole package).

KIND is `interface`, or for a native component its artifact type, `archive`
or `dylib` (CPS's names), with `--location` the artifact's path relative to
the prefix, `--soname` a shared library's install name (what the loader
looks up) and `--link-name` the name `-l` takes. `--pointer-size` is the
architecture's, for CMake's version-file check.

A `module` component (the `buildlog` format only: CMake and pkg-config
cannot state one, and CPS's consumers do not load it) is a file a loader
dlopens, with its `--location`; it is no default component. `--runtime-dep`
says that COMP needs the module MODULE at run time, placed PLACE
(`beside_loader` or `beside_executables`), so a consumer that links COMP gets
the module where COMP looks for it.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field

from . import dl_package

CPS_VERSION = "0.13.0"
FORMATS = (
    "cps",
    "pc",
    "cmake-config",
    "cmake-version",
    "cmake-targets",
    "buildlog-facts",
    "buildlog-manifest",
)
POLICIES = ("same_major", "same_minor", "exact", "any_newer")
NATIVE_KINDS = ("archive", "dylib")
BUILDLOG_ONLY_KINDS = ("module",)


class ExportError(Exception):
    pass


@dataclass
class Component:
    kind: str
    requires: list[str] = field(default_factory=list)
    # (package, component) of other packages; component "" is the package
    depends: list[tuple[str, str]] = field(default_factory=list)
    # (field, kind, value), kind: path (prefix-relative) | abspath | value
    props: list[tuple[str, str, str]] = field(default_factory=list)
    # (module, place): loaded at run time (buildlog format only)
    runtime: list[tuple[str, str]] = field(default_factory=list)
    location: str | None = None  # native: the artifact, relative to the prefix
    soname: str | None = None  # dylib: its install name
    link_name: str | None = None  # native: what -l takes

    def values(self, name: str) -> list[tuple[str, str]]:
        return [(k, v) for f, k, v in self.props if f == name]


@dataclass
class Package:
    fmt: str
    out: str
    name: str
    install_path: str
    version: str | None = None
    compat: str | None = None
    description: str | None = None
    arch_independent: bool = False
    pointer_size: str | None = None
    rules_manifest: str | None = None
    # prefix-relative path of the buildlog package's manifest, when this
    # install writes one: the CPS file names it as its sibling
    buildlog_manifest: str | None = None
    components: dict[str, Component] = field(default_factory=dict)
    # other packages this one requires, and their minimum versions
    depends_version: dict[str, str] = field(default_factory=dict)

    @property
    def depends(self) -> list[str]:
        return sorted({d for c in self.components.values() for d, _ in c.depends})

    @property
    def up(self) -> int:
        """How many directories the file's directory is below the prefix."""
        return len([p for p in os.path.dirname(self.install_path).split("/") if p])


def parse(argv: list[str]) -> Package:
    opts: dict[str, str] = {}
    flags: set[str] = set()
    comps: dict[str, Component] = {}
    versions: dict[str, str] = {}
    pending: list[tuple[str, str, str, str]] = []
    args = list(argv)
    while args:
        a = args.pop(0)
        if a in (
            "--format",
            "-o",
            "--package",
            "--install-path",
            "--version",
            "--compat",
            "--description",
            "--pointer-size",
            "--rules-manifest",
            "--buildlog-manifest",
        ):
            if not args:
                raise ExportError(f"{a} needs a value")
            opts[a] = args.pop(0)
        elif a == "--arch-independent":
            flags.add(a)
        elif a.startswith("--component="):
            kind, sep, name = a[len("--component=") :].partition(":")
            if not sep or not name or name in comps:
                raise ExportError(f"bad or repeated component {a}")
            comps[name] = Component(kind)
        elif a.startswith("--requires="):
            comp, sep, req = a[len("--requires=") :].partition(":")
            if not sep:
                raise ExportError(f"bad requirement {a}")
            pending.append(("requires", comp, "", req))
        elif a.startswith("--depends="):
            comp, sep1, rest = a[len("--depends=") :].partition(":")
            pkg, sep2, pcomp = rest.partition(":")
            if not (sep1 and sep2 and pkg):
                raise ExportError(f"bad requirement {a}")
            pending.append(("depends", comp, pkg, pcomp))
        elif a.startswith("--depends-version="):
            pkg, sep, v = a[len("--depends-version=") :].partition("=")
            if not (sep and pkg and v) or pkg in versions:
                raise ExportError(f"bad or repeated requirement version {a}")
            versions[pkg] = v
        elif a.startswith("--runtime-dep="):
            comp, sep1, rest = a[len("--runtime-dep=") :].partition(":")
            mod, sep2, place = rest.partition(":")
            if not (sep1 and sep2 and comp and mod and place):
                raise ExportError(f"bad run-time dependency {a}")
            pending.append(("runtime", comp, mod, place))
        elif re.match(r"--(location|soname|link-name)=", a):
            what, _, rest = a[2:].partition("=")
            comp, sep, value = rest.partition("=")
            if not (sep and comp and value):
                raise ExportError(f"bad {what} {a}")
            pending.append((what, comp, "", value))
        elif re.match(r"--(path|abspath|value)=", a):
            kind, _, rest = a[2:].partition("=")
            comp, sep1, rest = rest.partition(":")
            name, sep2, value = rest.partition("=")
            if not (sep1 and sep2 and name):
                raise ExportError(f"bad property {a}")
            pending.append((kind, comp, name, value))
        else:
            raise ExportError(f"unknown argument {a}")
    for key in ("--format", "-o", "--package", "--install-path"):
        if key not in opts:
            raise ExportError(f"missing {key}")
    if opts["--format"] not in FORMATS:
        raise ExportError(f"unknown format {opts['--format']} ({', '.join(FORMATS)})")
    if opts.get("--compat") and opts["--compat"] not in POLICIES:
        raise ExportError(f"unknown compatibility policy {opts['--compat']}")
    for name, c in comps.items():
        if c.kind not in NATIVE_KINDS + BUILDLOG_ONLY_KINDS and c.kind != "interface":
            raise ExportError(f"component {name}: unknown kind {c.kind}")
    for kind, comp, name, value in pending:
        if comp not in comps:
            raise ExportError(f"{kind} of an undeclared component {comp}")
        if kind == "requires":
            comps[comp].requires.append(value)
        elif kind == "depends":
            comps[comp].depends.append((name, value))
        elif kind == "runtime":
            if name not in comps or comps[name].kind != "module":
                raise ExportError(
                    f"run-time dependency of {comp} on {name}, which is no module component"
                )
            comps[comp].runtime.append((name, value))
        elif kind in ("location", "soname", "link-name"):
            attr = kind.replace("-", "_")
            if getattr(comps[comp], attr) is not None:
                raise ExportError(f"repeated {kind} of {comp}")
            setattr(comps[comp], attr, value)
        else:
            comps[comp].props.append((name, kind, value))
    for name, c in comps.items():
        if c.kind in NATIVE_KINDS and (c.location is None or c.link_name is None):
            raise ExportError(f"native component {name} needs --location and --link-name")
        if c.kind == "dylib" and c.soname is None:
            raise ExportError(f"shared library component {name} needs --soname")
        if c.kind == "interface" and (c.location or c.soname or c.link_name):
            raise ExportError(f"interface component {name} has no artifact")
        if c.kind == "module" and (c.location is None or c.soname or c.link_name):
            raise ExportError(f"module component {name} needs --location and nothing else")
        if opts["--format"] != "buildlog-facts" and (c.kind in BUILDLOG_ONLY_KINDS or c.runtime):
            raise ExportError(f"component {name}: run-time modules are only in the buildlog format")
    return Package(
        fmt=opts["--format"],
        out=opts["-o"],
        name=opts["--package"],
        install_path=opts["--install-path"],
        version=opts.get("--version"),
        compat=opts.get("--compat"),
        description=opts.get("--description"),
        arch_independent="--arch-independent" in flags,
        pointer_size=opts.get("--pointer-size"),
        rules_manifest=opts.get("--rules-manifest"),
        buildlog_manifest=opts.get("--buildlog-manifest"),
        components=comps,
        depends_version=versions,
    )


def _split_define(d: str) -> tuple[str, str | None]:
    name, eq, value = d.partition("=")
    return name, (value if eq else None)


def _version_parts(v: str) -> list[str]:
    return v.split(".")


# ---- CPS ----------------------------------------------------------------------


def _compat_version(p: Package) -> str | None:
    if p.version is None or p.compat in (None, "exact"):
        return None
    parts = _version_parts(p.version)
    if p.compat == "same_major":
        return ".".join([parts[0], *["0"] * (len(parts) - 1)])
    if p.compat == "same_minor":
        return ".".join([*parts[:2], *["0"] * (len(parts) - 2)])
    return ".".join(["0"] * len(parts))  # any_newer


def write_cps(p: Package) -> str:
    prefix_dir = os.path.dirname(p.install_path)
    doc: dict = {"name": p.name, "cps_version": CPS_VERSION}
    if p.version:
        doc["version"] = p.version
        compat = _compat_version(p)
        if compat is not None:
            doc["compat_version"] = compat
    if p.description:
        doc["description"] = p.description
    doc["cps_path"] = f"@prefix@/{prefix_dir}"
    if p.buildlog_manifest:
        doc["x_buildlog_manifest"] = p.buildlog_manifest
    if p.depends:
        doc["requires"] = {
            d: ({"version": p.depends_version[d]} if d in p.depends_version else {})
            for d in p.depends
        }
    doc["default_components"] = sorted(p.components)
    comps: dict = {}
    for name, c in sorted(p.components.items()):
        out: dict = {"type": c.kind}
        if c.location is not None:
            out["location"] = f"@prefix@/{c.location}"
        reqs = [f":{r}" for r in sorted(c.requires)]
        reqs += [f"{d}:{k}" if k else d for d, k in sorted(c.depends)]
        if reqs:
            out["requires"] = reqs
        for fname in sorted({f for f, _, _ in c.props}):
            items = c.values(fname)
            if fname == "definitions":
                defs: dict = {}
                for _, d in sorted(items):
                    n, v = _split_define(d)
                    defs[n] = v
                out[fname] = {"*": defs}
            else:
                out[fname] = [f"@prefix@/{v}" if k == "path" else v for k, v in sorted(items)]
        comps[name] = out
    doc["components"] = comps
    return json.dumps(doc, indent=2) + "\n"


# ---- buildlog (Datalog) -------------------------------------------------------


def write_buildlog_facts(p: Package) -> str:
    fact = dl_package.fact
    lines = [
        f"// {p.name}: the facts of an installed package, in buildlog's vocabulary (dl_package.py).",
        "// Paths named `rel` are relative to the install prefix (pkg_prefix finds it from this file).",
        fact("pkg_package", p.name),
        fact("pkg_prefix", "/".join([".."] * p.up) or "."),
    ]
    if p.version:
        lines.append(fact("pkg_version", p.name, p.version))
    if p.compat:
        lines.append(fact("pkg_compat", p.name, p.compat))
    if p.description:
        lines.append(fact("pkg_description", p.name, p.description))
    for dep, minimum in sorted(p.depends_version.items()):
        lines.append(fact("pkg_requires_version", dep, minimum))
    for name, c in sorted(p.components.items()):
        lines.append(fact("pkg_component", name, c.kind))
        if c.kind != "module":
            lines.append(fact("pkg_default", name))
        if c.location is not None:
            lines.append(fact("pkg_location", name, c.location))
        if c.link_name is not None:
            lines.append(fact("pkg_link_name", name, c.link_name))
        if c.soname is not None:
            lines.append(fact("pkg_soname", name, c.soname))
        lines += [fact("pkg_requires", name, r) for r in sorted(c.requires)]
        lines += [fact("pkg_depends", name, d, k) for d, k in sorted(c.depends)]
        lines += [fact("pkg_runtime_dep", name, m, place) for m, place in sorted(c.runtime)]
        rel = {"path": "pkg_prop_path", "abspath": "pkg_prop_abs", "value": "pkg_prop"}
        lines += [fact(rel[k], name, f, v) for f, k, v in sorted(c.props)]
    return "\n".join(lines) + "\n"


def write_buildlog_manifest(p: Package) -> str:
    base: dict = {}
    if p.rules_manifest:
        with open(p.rules_manifest, "rb") as f:
            base = tomllib.load(f)
        if base.get("name") != p.name:
            raise ExportError(
                f"the rule package {p.rules_manifest} is named {base.get('name')!r}, "
                f"not like the exported package {p.name!r}"
            )
    version = p.version or base.get("version") or "0"
    try:
        data = dl_package.installed_manifest(p.name, str(version), base)
        return dl_package.write_manifest(data)
    except (dl_package.FactsError, ValueError) as exc:
        raise ExportError(str(exc)) from exc


# ---- pkg-config -----------------------------------------------------------------


def _pc_escape(s: str) -> str:
    return s.replace("$", "$$").replace(" ", "\\ ")


def write_pc(p: Package) -> str:
    """One module for the package: the union of its components (pkg-config
    has no components). Requirements between them are internal."""
    up = "/".join([".."] * p.up)
    lines = [f"prefix=${{pcfiledir}}/{up}" if up else "prefix=${pcfiledir}"]
    cflags: list[str] = []
    libs: list[str] = []
    for _name, c in sorted(p.components.items()):
        if c.location is not None:
            libdir = f"-L${{prefix}}/{_pc_escape(os.path.dirname(c.location))}"
            if libdir not in libs:
                libs.append(libdir)
            libs.append(f"-l{_pc_escape(c.link_name or '')}")
        for f, k, v in sorted(c.props):
            if f == "include":
                path = f"${{prefix}}/{_pc_escape(v)}" if k == "path" else _pc_escape(v)
                if f"-I{path}" not in cflags:
                    cflags.append(f"-I{path}")
            elif f == "define":
                if f"-D{_pc_escape(v)}" not in cflags:
                    cflags.append(f"-D{_pc_escape(v)}")
            elif f == "cflag":
                cflags.append(_pc_escape(v))
            else:
                raise ExportError(f"pkg-config has no field {f}")
    lines += [
        "",
        f"Name: {p.name}",
        f"Description: {p.description or p.name}",
        f"Version: {p.version or ''}".rstrip(),
    ]
    reqs = [f"{d} >= {p.depends_version[d]}" if d in p.depends_version else d for d in p.depends]
    if reqs:
        lines.append("Requires: " + ", ".join(reqs))
    if libs:
        lines.append("Libs: " + " ".join(libs))
    if cflags:
        lines.append("Cflags: " + " ".join(cflags))
    return "\n".join(lines) + "\n"


# ---- CMake --------------------------------------------------------------------


def _cmake_quote(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def write_cmake_config(p: Package) -> str:
    targets = " ".join(f"{p.name}::{c}" for c in sorted(p.components))
    first = f"{p.name}::{min(p.components)}"
    deps = ""
    if p.depends:
        deps = (
            "include(CMakeFindDependencyMacro)\n"
            + "".join(
                f"find_dependency({d} {p.depends_version[d]})\n"
                if d in p.depends_version
                else f"find_dependency({d})\n"
                for d in p.depends
            )
            + "\n"
        )
    return (
        f"# {p.name}: CMake package configuration, written by buildlog "
        "(buildlog -E export_package).\n"
        f"{deps}"
        "include(FindPackageHandleStandardArgs)\n"
        "set(${CMAKE_FIND_PACKAGE_NAME}_CONFIG ${CMAKE_CURRENT_LIST_FILE})\n"
        "find_package_handle_standard_args(${CMAKE_FIND_PACKAGE_NAME} CONFIG_MODE)\n"
        "\n"
        f"# Imported targets: {targets}\n"
        f"if(NOT TARGET {first})\n"
        f'  include("${{CMAKE_CURRENT_LIST_DIR}}/{p.name}Targets.cmake")\n'
        "endif()\n"
    )


def write_cmake_version(p: Package) -> str:
    if p.version is None:
        raise ExportError("a CMake package version file needs --version")
    if not p.arch_independent and p.pointer_size is None:
        raise ExportError(
            "the version file of an architecture-dependent package needs --pointer-size"
        )
    v = p.version
    parts = _version_parts(v)
    if p.arch_independent:
        arch = [
            "# The package has no native artifacts (architecture-independent), so",
            "# there is no pointer-size check (nlohmann/json #1697).",
        ]
    else:
        arch = ["# The package has native artifacts: the consumer's pointer size must match."]
    head = [
        f"# {p.name} {v}: CMake package version file, written by buildlog.",
        f"# Compatibility: {p.compat or 'exact'}.",
        *arch,
        f'set(PACKAGE_VERSION "{v}")',
        "",
    ]
    if p.compat in (None, "exact"):
        body = [
            "if(PACKAGE_FIND_VERSION VERSION_EQUAL PACKAGE_VERSION)",
            "  set(PACKAGE_VERSION_COMPATIBLE TRUE)",
            "  set(PACKAGE_VERSION_EXACT TRUE)",
            "else()",
            "  set(PACKAGE_VERSION_COMPATIBLE FALSE)",
            "endif()",
        ]
        return "\n".join(head + body + _cmake_pointer_check(p)) + "\n"
    if p.compat == "same_major":
        check = f'  if(PACKAGE_FIND_VERSION_MAJOR STREQUAL "{parts[0]}")'
    elif p.compat == "same_minor":
        minor = parts[1] if len(parts) > 1 else "0"
        check = (
            f'  if(PACKAGE_FIND_VERSION_MAJOR STREQUAL "{parts[0]}" AND '
            f'PACKAGE_FIND_VERSION_MINOR STREQUAL "{minor}")'
        )
    else:
        check = "  if(TRUE)"
    body = [
        "if(PACKAGE_VERSION VERSION_LESS PACKAGE_FIND_VERSION)",
        "  set(PACKAGE_VERSION_COMPATIBLE FALSE)",
        "else()",
        check,
        "    set(PACKAGE_VERSION_COMPATIBLE TRUE)",
        "  else()",
        "    set(PACKAGE_VERSION_COMPATIBLE FALSE)",
        "  endif()",
        "  if(PACKAGE_FIND_VERSION STREQUAL PACKAGE_VERSION)",
        "    set(PACKAGE_VERSION_EXACT TRUE)",
        "  endif()",
        "endif()",
    ]
    return "\n".join(head + body + _cmake_pointer_check(p)) + "\n"


def _cmake_pointer_check(p: Package) -> list[str]:
    """write_basic_package_version_file's check, for native packages."""
    if p.arch_independent:
        return []
    n = p.pointer_size
    return [
        "",
        "# if the installed or the using project don't have CMAKE_SIZEOF_VOID_P set, ignore it:",
        f'if("${{CMAKE_SIZEOF_VOID_P}}" STREQUAL "" OR "{n}" STREQUAL "")',
        "  return()",
        "endif()",
        "",
        "# check that the installed version has the same 32/64bit-ness as the one which is currently searching:",
        f'if(NOT CMAKE_SIZEOF_VOID_P STREQUAL "{n}")',
        f'  math(EXPR installedBits "{n} * 8")',
        '  set(PACKAGE_VERSION "${PACKAGE_VERSION} (${installedBits}bit)")',
        "  set(PACKAGE_VERSION_UNSUITABLE TRUE)",
        "endif()",
    ]


CMAKE_KINDS = {"interface": "INTERFACE", "archive": "STATIC", "dylib": "SHARED"}

CMAKE_PROPS = {
    "INTERFACE_COMPILE_DEFINITIONS",
    "INTERFACE_COMPILE_FEATURES",
    "INTERFACE_COMPILE_OPTIONS",
    "INTERFACE_INCLUDE_DIRECTORIES",
    "INTERFACE_SYSTEM_INCLUDE_DIRECTORIES",
}


def write_cmake_targets(p: Package) -> str:
    """CMake's install(EXPORT) file, in the template CMake generates (so
    CMake and buildlog's own CMake export importer both read it)."""
    names = [f"{p.name}::{c}" for c in sorted(p.components)]
    out = [
        "# Generated by buildlog (buildlog -E export_package), in the template of",
        "# CMake's install(EXPORT).",
        "",
        'if("${CMAKE_MAJOR_VERSION}.${CMAKE_MINOR_VERSION}" LESS 2.8)',
        '   message(FATAL_ERROR "CMake >= 3.0.0 required")',
        "endif()",
        'if(CMAKE_VERSION VERSION_LESS "3.0.0")',
        '   message(FATAL_ERROR "CMake >= 3.0.0 required")',
        "endif()",
        "cmake_policy(PUSH)",
        "cmake_policy(VERSION 3.0.0...3.31)",
        "#----------------------------------------------------------------",
        "# Generated CMake target import file.",
        "#----------------------------------------------------------------",
        "",
        "# Commands may need to know the format version.",
        "set(CMAKE_IMPORT_FILE_VERSION 1)",
        "",
        "# Protect against multiple inclusion, which would fail when already imported targets are added once more.",
        'set(_cmake_targets_defined "")',
        'set(_cmake_targets_not_defined "")',
        'set(_cmake_expected_targets "")',
        f"foreach(_cmake_expected_target IN ITEMS {' '.join(names)})",
        '  list(APPEND _cmake_expected_targets "${_cmake_expected_target}")',
        '  if(TARGET "${_cmake_expected_target}")',
        '    list(APPEND _cmake_targets_defined "${_cmake_expected_target}")',
        "  else()",
        '    list(APPEND _cmake_targets_not_defined "${_cmake_expected_target}")',
        "  endif()",
        "endforeach()",
        "unset(_cmake_expected_target)",
        "if(_cmake_targets_defined STREQUAL _cmake_expected_targets)",
        "  unset(_cmake_targets_defined)",
        "  unset(_cmake_targets_not_defined)",
        "  unset(_cmake_expected_targets)",
        "  unset(CMAKE_IMPORT_FILE_VERSION)",
        "  cmake_policy(POP)",
        "  return()",
        "endif()",
        'if(NOT _cmake_targets_defined STREQUAL "")',
        '  string(REPLACE ";" ", " _cmake_targets_defined_text "${_cmake_targets_defined}")',
        '  string(REPLACE ";" ", " _cmake_targets_not_defined_text "${_cmake_targets_not_defined}")',
        (
            '  message(FATAL_ERROR "Some (but not all) targets in this export set were already '
            "defined.\\nTargets Defined: ${_cmake_targets_defined_text}\\nTargets not yet "
            'defined: ${_cmake_targets_not_defined_text}\\n")'
        ),
        "endif()",
        "unset(_cmake_targets_defined)",
        "unset(_cmake_targets_not_defined)",
        "unset(_cmake_expected_targets)",
        "",
        "",
        "# Compute the installation prefix relative to this file.",
        'get_filename_component(_IMPORT_PREFIX "${CMAKE_CURRENT_LIST_FILE}" PATH)',
    ]
    out += ['get_filename_component(_IMPORT_PREFIX "${_IMPORT_PREFIX}" PATH)'] * p.up
    out += [
        'if(_IMPORT_PREFIX STREQUAL "/")',
        '  set(_IMPORT_PREFIX "")',
        "endif()",
        "",
    ]
    for comp, c in sorted(p.components.items()):
        t = f"{p.name}::{comp}"
        props: dict[str, list[str]] = {}
        for f, k, v in sorted(c.props):
            if f not in CMAKE_PROPS:
                raise ExportError(f"CMake has no target property {f} here")
            spelled = "${_IMPORT_PREFIX}/" + _cmake_quote(v) if k == "path" else _cmake_quote(v)
            props.setdefault(f, []).append(spelled)
        links = [f"{p.name}::{r}" for r in sorted(c.requires)]
        links += [f"{d}::{k or d}" for d, k in sorted(c.depends)]
        if links:
            props["INTERFACE_LINK_LIBRARIES"] = links
        kind = CMAKE_KINDS[c.kind]
        out += [f"# Create imported target {t}", f"add_library({t} {kind} IMPORTED)", ""]
        if props:
            out.append(f"set_target_properties({t} PROPERTIES")
            for name in sorted(props):
                out.append(f'  {name} "{";".join(props[name])}"')
            out += [")", ""]
        if c.location is not None:
            loc = "${_IMPORT_PREFIX}/" + _cmake_quote(c.location)
            out += [f"set_target_properties({t} PROPERTIES", f'  IMPORTED_LOCATION "{loc}"']
            if c.soname is not None:
                out.append(f'  IMPORTED_SONAME "{_cmake_quote(c.soname)}"')
            out += [
                "  )",
                f"list(APPEND _cmake_import_check_targets {t} )",
                f'list(APPEND _cmake_import_check_files_for_{t} "{loc}" )',
                "",
            ]
    out += [
        "# Load information for each installed configuration.",
        f'file(GLOB _cmake_config_files "${{CMAKE_CURRENT_LIST_DIR}}/{p.name}Targets-*.cmake")',
        "foreach(_cmake_config_file IN LISTS _cmake_config_files)",
        '  include("${_cmake_config_file}")',
        "endforeach()",
        "unset(_cmake_config_file)",
        "unset(_cmake_config_files)",
        "",
        "# Cleanup temporary variables.",
        "set(_IMPORT_PREFIX)",
        "",
        "# Loop over all imported files and verify that they actually exist",
        "foreach(_cmake_target IN LISTS _cmake_import_check_targets)",
        '  if(CMAKE_VERSION VERSION_LESS "3.28"',
        "      OR NOT DEFINED _cmake_import_check_xcframework_for_${_cmake_target}",
        '      OR NOT IS_DIRECTORY "${_cmake_import_check_xcframework_for_${_cmake_target}}")',
        '    foreach(_cmake_file IN LISTS "_cmake_import_check_files_for_${_cmake_target}")',
        '      if(NOT EXISTS "${_cmake_file}")',
        '        message(FATAL_ERROR "The imported target \\"${_cmake_target}\\" references the file',
        '   \\"${_cmake_file}\\"',
        "but this file does not exist.  Possible reasons include:",
        "* The file was deleted, renamed, or moved to another location.",
        "* An install or uninstall procedure did not complete successfully.",
        "* The installation package was faulty and contained",
        '   \\"${CMAKE_CURRENT_LIST_FILE}\\"',
        "but not all the files it references.",
        '")',
        "      endif()",
        "    endforeach()",
        "  endif()",
        "  unset(_cmake_file)",
        '  unset("_cmake_import_check_files_for_${_cmake_target}")',
        "endforeach()",
        "unset(_cmake_target)",
        "unset(_cmake_import_check_targets)",
        "",
        "# This file does not depend on other imported targets which have",
        "# been exported from the same project but in a separate export set.",
        "",
        "# Commands beyond this point should not need to know the version.",
        "set(CMAKE_IMPORT_FILE_VERSION)",
        "cmake_policy(POP)",
    ]
    return "\n".join(out) + "\n"


WRITERS = {
    "cps": write_cps,
    "pc": write_pc,
    "cmake-config": write_cmake_config,
    "cmake-version": write_cmake_version,
    "cmake-targets": write_cmake_targets,
    "buildlog-facts": write_buildlog_facts,
    "buildlog-manifest": write_buildlog_manifest,
}


def export_package(*argv: str) -> None:
    p = parse(list(argv))
    text = WRITERS[p.fmt](p)
    if os.path.isfile(p.out):
        with open(p.out, newline="") as f:
            if f.read() == text:
                return
    os.makedirs(os.path.dirname(p.out) or ".", exist_ok=True)
    with open(p.out, "w", newline="") as f:
        f.write(text)
