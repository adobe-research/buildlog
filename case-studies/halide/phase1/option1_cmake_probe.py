# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Option 1 prototype: wrap a probing CMake configure and turn its dump into
buildlog fact rows (the `res_pkg_*` vocabulary of buildlog-cpp's importers).

    uv run --frozen python option1_cmake_probe.py inputs/native-arm64.json [--fresh]

Steps, all generic except the registration (the `registration` object in the
inputs JSON: which find module, which roots, which variables, name mapping):
1. write a toolchain file from buildlog-side facts (compiler, sysroot, triple);
2. configure `llvm-probe/` with a JSON trace (file:line of every command);
3. rebuild each dumped target as the importer's `TargetInfo` and run the
   CMake-export importer's own property/genex mapping (`cmake_export._emit`),
   so the rows mean exactly what imported export files mean;
4. attribute each row to the command that set the property (from the trace)
   and to the variables that command read (their last writers).

Outputs go to raw/option1-<name>/ (gitignored): rows.json, the probe dump,
the trace. Prints a summary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from buildlog_cpp.importers import cmake_export as cx
from buildlog_cpp.importers.common import Result

HERE = Path(__file__).resolve().parent
PROBE = HERE / "llvm-probe"

# ---- 1. toolchain file -------------------------------------------------------

ARCHS = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64"}


def toolchain_file(inp: dict) -> str:
    arch, _, _rest = inp["triple"].partition("-")
    host = os.uname().machine
    lines = [
        "# Generated from buildlog facts; do not edit.",
        f'set(CMAKE_C_COMPILER "{inp["cc"]}")',
        f'set(CMAKE_CXX_COMPILER "{inp["cxx"]}")',
        f'set(CMAKE_OSX_ARCHITECTURES "{ARCHS[arch]}")',
    ]
    if inp.get("sysroot"):
        lines.append(f'set(CMAKE_OSX_SYSROOT "{inp["sysroot"]}")')
    if ARCHS[arch] != ARCHS.get(host, host):
        # Setting CMAKE_SYSTEM_NAME is what makes CMake treat this as a cross build.
        lines += ["set(CMAKE_SYSTEM_NAME Darwin)", f"set(CMAKE_SYSTEM_PROCESSOR {ARCHS[arch]})"]
    return "\n".join(lines) + "\n"


# ---- 2. the probe configure --------------------------------------------------


def configure(inp: dict, out: Path, fresh: bool) -> tuple[float, Path]:
    build = out / "build"
    if fresh and build.exists():
        shutil.rmtree(build)
    out.mkdir(parents=True, exist_ok=True)
    tc = out / "toolchain.cmake"
    tc.write_text(toolchain_file(inp))
    trace = out / "trace.jsonl"
    reg = inp["registration"]
    cache = {k: v.format(**inp) for k, v in reg.get("cache", {}).items()}
    cmd = [
        "cmake",
        "-S",
        str(PROBE),
        "-B",
        str(build),
        "-G",
        "Ninja",
        f"-DCMAKE_BUILD_TYPE={inp['build_type']}",
        f"-DCMAKE_TOOLCHAIN_FILE={tc}",
        f"-DPROBE_MODULE_PATH={Path(reg['module_path']).expanduser()}",
        f"-DPROBE_PACKAGE={reg['package']}",
        f"-DPROBE_ARGS={';'.join(reg['args'])}",
        f"-DPROBE_ROOT_REGEX={reg.get('root_regex', '')}",
        f"-DPROBE_ROOTS={';'.join(reg.get('roots', []))}",
        f"-DPROBE_VAR_REGEX={reg.get('var_regex', '')}",
        f"-DPROBE_OUT={out / 'probe'}",
        *(f"-D{k}={v}" for k, v in sorted(cache.items())),
        "--trace-format=json-v1",
        "--trace-expand",
        f"--trace-redirect={trace}",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    dt = time.perf_counter() - t0
    (out / "configure.log").write_text(proc.stdout + proc.stderr)
    if proc.returncode:
        sys.exit(f"probe configure failed, see {out / 'configure.log'}")
    return dt, trace


# ---- 3. provenance from the trace --------------------------------------------

PROP_CMDS = {
    "target_compile_definitions": "INTERFACE_COMPILE_DEFINITIONS",
    "target_include_directories": "INTERFACE_INCLUDE_DIRECTORIES",
    "target_link_libraries": "INTERFACE_LINK_LIBRARIES",
    "target_compile_options": "INTERFACE_COMPILE_OPTIONS",
    "target_link_options": "INTERFACE_LINK_OPTIONS",
}
_VARREF = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


class Trace:
    """Which command last set each (target, property), and which variables
    that command read, with their last writers. One hop of data flow only."""

    def __init__(self, path: Path):
        self.defined: dict[str, tuple[str, int]] = {}
        self.setters: dict[tuple[str, str], tuple[str, int, int]] = {}
        self.reads: dict[tuple[str, int], list[tuple[str, str]]] = {}
        writers: dict[str, str] = {}
        sources: dict[str, list[str]] = {}
        for line in path.read_text().splitlines()[1:]:
            ev = json.loads(line)
            cmd, args, f, ln = ev["cmd"].lower(), ev["args"], ev["file"], ev["line"]
            end = ev.get("line_end", ln)
            if cmd in ("set", "list", "string", "separate_arguments", "get_filename_component"):
                var = self._written(cmd, args)
                if var:
                    writers[var] = f"{f}:{ln}"
            if cmd in ("add_library", "add_executable") and "IMPORTED" in args:
                self.defined[args[0]] = (f, ln)
                self._reads(f, ln, end, writers, sources)
            elif cmd in PROP_CMDS and args:
                self.setters[(args[0], PROP_CMDS[cmd])] = (f, ln, end)
                self._reads(f, ln, end, writers, sources)
            elif cmd == "set_property" and args[:1] == ["TARGET"] and "PROPERTY" in args:
                i = args.index("PROPERTY")
                for t in args[1 : args.index("APPEND") if "APPEND" in args else i]:
                    self.setters[(t, args[i + 1])] = (f, ln, end)
                self._reads(f, ln, end, writers, sources)
            elif cmd == "set_target_properties" and "PROPERTIES" in args:
                i = args.index("PROPERTIES")
                for t in args[:i]:
                    for k in args[i + 1 :: 2]:
                        self.setters[(t, k)] = (f, ln, end)
                self._reads(f, ln, end, writers, sources)
        self.last_writer = writers

    @staticmethod
    def _written(cmd: str, args: list[str]) -> str | None:
        if not args:
            return None
        if cmd in ("set", "separate_arguments", "get_filename_component"):
            return args[0]
        if cmd == "list" and len(args) > 1 and args[0] in ("APPEND", "PREPEND", "REMOVE_ITEM"):
            return args[1]
        if cmd == "string" and len(args) > 1 and args[0] in ("APPEND", "CONFIGURE"):
            return args[1] if args[0] == "APPEND" else args[2] if len(args) > 2 else None
        return None

    def _reads(self, f, ln, end, writers, sources) -> None:
        if f not in sources:
            try:
                sources[f] = Path(f).read_text(errors="replace").splitlines()
            except OSError:
                sources[f] = []
        text = "\n".join(sources[f][ln - 1 : end])
        self.reads[(f, ln)] = [(v, writers.get(v, "?")) for v in _VARREF.findall(text)]


# ---- 4. dump -> importer TargetInfo -> rows -----------------------------------

KINDS = {
    "SHARED_LIBRARY": "SHARED",
    "STATIC_LIBRARY": "STATIC",
    "UNKNOWN_LIBRARY": "UNKNOWN",
    "MODULE_LIBRARY": "MODULE",
    "INTERFACE_LIBRARY": "INTERFACE",
}
SKIP = {"IMPORTED", "IMPORTED_GLOBAL", "IMPORTED_CONFIGURATIONS"}


def _arg(value: str, line: int) -> cx.Arg:
    return cx.Arg(value.replace("\\", "\\\\").replace("${", "\\${"), True, line)


def to_rows(dump: dict, trace: Trace, reg: dict, os_name: str) -> tuple[dict, dict]:
    infos: dict[str, cx.TargetInfo] = {}
    files: dict[str, Path] = {}
    for name, t in dump["targets"].items():
        f, ln = trace.defined.get(name, ("<unknown>", 0))
        kind = "executable" if t["type"] == "EXECUTABLE" else cx.LIB_KINDS[KINDS[t["type"]]]
        info = cx.TargetInfo(name, kind, ln)
        configs: dict[str, list] = {}
        for prop, value in t["props"].items():
            if prop in SKIP or value == "":
                continue
            sf, sl, _ = trace.setters.get((name, prop), (f, ln, ln))
            m = re.fullmatch(r"(IMPORTED_[A-Z_]+?)_([A-Z0-9_]+)", prop)
            cfgs = t["props"].get("IMPORTED_CONFIGURATIONS", "").upper().split(";")
            if m and m.group(2) in cfgs:
                configs.setdefault(m.group(2), []).append((prop, _arg(value, sl), Path(sf)))
            else:
                info.props.append((prop, _arg(value, sl)))
                files.setdefault(name, Path(sf))
        for cfg, props in configs.items():
            info.configs[cfg] = ([(p, a) for p, a, _ in props], props[0][2])
        infos[name] = info
        files.setdefault(name, Path(f))

    res = Result("probe:" + reg["name"], Path(reg["module_path"]).expanduser())
    res.rows["res_probe_setting"] = []
    env = cx.Env("install", {os_name})
    for name, info in infos.items():
        ex = cx.ExportFile(files[name], "install", None, infos, [], [])
        cx._emit(res, ex, env, info)
    # Registered variables become settings rows (a relation the importers lack).
    for var, key in reg["variables"].items():
        value = dump["variables"].get(var)
        if value is not None:
            f, _, ln = trace.last_writer.get(var, ":").rpartition(":")
            res.current = Path(f) if f else None
            res.add("res_probe_setting", key, value, line=int(ln) if ln else None)
    res.current = None
    out = res.finish()
    rn = reg.get("rename", {})

    def rename(rel: str, row: tuple) -> tuple:
        # Target-id columns only: t (1) everywhere, d (2) of deps.
        cols = (1, 2) if rel == "res_pkg_dep" else (1,) if rel != "res_probe_setting" else ()
        return tuple(rn.get(x, x) if i in cols else x for i, x in enumerate(row))

    rows = {
        rel: [rename(rel, r) for r in rs] for rel, rs in out.items() if rel != "__origin__" and rs
    }
    origins: dict[str, list] = {}
    for rel, row, origin in out["__origin__"]:
        key = rel + "\t" + "\t".join(rename(rel, row)[1:])
        f, _, ln = origin.rpartition(":")
        reads = trace.reads.get((f, int(ln))) if ln.isdigit() else None
        origins.setdefault(key, []).append({"at": origin, "reads": reads or []})
    return rows, origins


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs")
    ap.add_argument("--fresh", action="store_true", help="delete the probe build tree first")
    a = ap.parse_args()
    inp = json.loads(Path(a.inputs).read_text())
    reg = inp["registration"]
    # module_path is relative to the inputs file (../../upstream/cmake: the submodule's).
    reg["module_path"] = str((Path(a.inputs).resolve().parent / reg["module_path"]).resolve())
    out = HERE / "raw" / f"option1-{inp['name']}"
    dt, trace_path = configure(inp, out, a.fresh)
    t0 = time.perf_counter()
    dump = json.loads((out / "probe" / "dump.json").read_text())
    trace = Trace(trace_path)
    os_name = "macos" if inp["triple"].split("-")[1:2] == ["apple"] else "linux"
    rows, origins = to_rows(dump, trace, inp["registration"], os_name)
    dconv = time.perf_counter() - t0
    (out / "rows.json").write_text(json.dumps({"rows": rows, "origins": origins}, indent=1))
    print(f"configure {dt:.2f}s, convert {dconv:.2f}s, {len(dump['targets'])} targets dumped")
    for rel, rs in sorted(rows.items()):
        print(f"  {rel}: {len(rs)}")
    print(f"rows: {out / 'rows.json'}")


if __name__ == "__main__":
    main()
