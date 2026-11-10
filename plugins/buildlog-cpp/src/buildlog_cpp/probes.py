#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery probes of the buildlog-cpp plugin (macOS: Apple clang and an
upstream LLVM; Linux: GCC and clang from PATH).

This module is intentionally small and boring: Soufflé derives typed request
relations, while this Python code executes the matching probe and returns typed
result facts. The buildlog driver owns iteration and caching.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import platform as pyplatform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from buildlog.discovery import WATCH_KEY, watch_identity
from buildlog.facts import text_lines

PLATFORMS = [
    ("arm64-macos", "arm64-apple-macos13"),
    ("x86_64-macos", "x86_64-apple-macos13"),
]
CONSTRAINTS = [
    ("arm64-macos", "cpu", "arm64"),
    ("arm64-macos", "os", "macos"),
    ("x86_64-macos", "cpu", "x86_64"),
    ("x86_64-macos", "os", "macos"),
]
SLICES = [("universal2-macos", "arm64-macos"), ("universal2-macos", "x86_64-macos")]
# Linux (GNU): the platforms are named like macOS's, cpu-os; the triple is
# the GNU multiarch one, which is also what gcc -dumpmachine prints.
LINUX_PLATFORMS = [
    ("arm64-linux", "aarch64-linux-gnu"),
    ("x86_64-linux", "x86_64-linux-gnu"),
]
LINUX_CONSTRAINTS = [
    ("arm64-linux", "cpu", "arm64"),
    ("arm64-linux", "os", "linux"),
    ("x86_64-linux", "cpu", "x86_64"),
    ("x86_64-linux", "os", "linux"),
]


def which(argv: list[str]) -> str | None:
    try:
        return subprocess.check_output(argv, text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def host_platform() -> str:
    m = {"aarch64": "arm64"}.get(pyplatform.machine(), pyplatform.machine())
    os_name = {"darwin": "macos", "linux": "linux"}.get(sys.platform)
    if os_name is None or m not in ("arm64", "x86_64"):
        raise RuntimeError(
            f"buildlog-cpp supports macOS and Linux on arm64/x86_64, got {sys.platform}/{m}"
        )
    return f"{m}-{os_name}"


def tool_version(cxx: str) -> str:
    out = which([cxx, "--version"]) or "unknown version"
    first = out.splitlines()[0]
    return first.split("version")[-1].split()[0] if "version" in first else first.split()[-1]


def candidate_toolchains() -> list[tuple[str, str, str, str, str]]:
    out = []
    if sys.platform == "linux":
        # The distribution's GCC, as `gcc` on PATH; clang too if
        # BUILDLOG_LINUX_TOOLCHAINS names it (like BUILDLOG_LLVM_BIN on
        # macOS), so an unpinned configure is not ambiguous.
        wanted = os.environ.get("BUILDLOG_LINUX_TOOLCHAINS", "gcc").split(",")
        gxx, gcc = shutil.which("g++"), shutil.which("gcc")
        if "gcc" in wanted and gxx and gcc:
            out.append(
                ("gcc", "gcc", gxx, gcc, shutil.which("gcc-ar") or shutil.which("ar") or "ar")
            )
        cxx, cc = shutil.which("clang++"), shutil.which("clang")
        if "clang" in wanted and cxx and cc:
            out.append(
                ("clang", "clang", cxx, cc, shutil.which("llvm-ar") or shutil.which("ar") or "ar")
            )
        return sorted(out)
    apple_cxx = which(["xcrun", "-f", "clang++"])
    if apple_cxx:
        out.append(
            (
                "apple-clang",
                "clang",
                apple_cxx,
                which(["xcrun", "-f", "clang"]) or apple_cxx,
                which(["xcrun", "-f", "ar"]) or "ar",
            )
        )
    # An upstream LLVM install, if the environment names its bin directory.
    llvm = Path(os.environ.get("BUILDLOG_LLVM_BIN", "/nonexistent"))
    if (llvm / "clang++").exists():
        out.append(
            (
                "llvm",
                "clang",
                str(llvm / "clang++"),
                str(llvm / "clang"),
                str(llvm / "llvm-ar"),
            )
        )
    return sorted(out)


def safe_dir(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def compile_probe(ctx, argv: list[str], src: str, suffix: str, key: str) -> bool:
    d = ctx.workdir / safe_dir(key)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("probe" + suffix)
    p.write_text(src)
    r = subprocess.run(
        argv + [str(p), "-o", str(d / "a.out")], capture_output=True, cwd=d, check=False
    )
    shutil.rmtree(d, ignore_errors=True)
    return r.returncode == 0


def inventory(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    host = host_platform()
    rows: dict[str, list[tuple[str, ...]]] = {
        "res_host_platform": [(req_id, host)],
        "res_platform": [],
        "res_platform_sdk": [],
        "res_platform_slice": [],
        "res_platform_constraint": [],
        "res_platform_tool": [],
        "res_toolchain": [],
        "res_toolchain_tool": [],
        "res_toolchain_runs_on": [],
        "res_platform_distro": [],
    }
    if sys.platform == "linux":
        rows["res_platform"] = [(req_id, p, t) for p, t in LINUX_PLATFORMS]
        rows["res_platform_constraint"] = [(req_id, p, k, v) for p, k, v in LINUX_CONSTRAINTS]
        # The host's distribution (os-release ID and ID_LIKE), for the
        # libdir default; a cross platform's is the user's to state.
        rows["res_platform_distro"] = [(req_id, host, f) for f in os_release_families()]
    else:
        if not shutil.which("xcrun"):
            raise RuntimeError("xcrun not found")
        sdk = which(["xcrun", "--show-sdk-path"]) or ""
        lipo = which(["xcrun", "-f", "lipo"])
        rows["res_platform"] = [(req_id, p, t) for p, t in PLATFORMS]
        rows["res_platform_sdk"] = [(req_id, p, sdk) for p, _ in PLATFORMS]
        rows["res_platform_slice"] = [(req_id, u, p) for u, p in SLICES]
        rows["res_platform_constraint"] = [(req_id, p, k, v) for p, k, v in CONSTRAINTS]
        if lipo:
            rows["res_platform_tool"] = [(req_id, u, "lipo", lipo) for u, _ in SLICES]
    for tc, family, cxx, cc, ar in candidate_toolchains():
        rows["res_toolchain"].append((req_id, tc, family, tool_version(cxx)))
        rows["res_toolchain_tool"].extend(
            [(req_id, tc, "cxx", cxx), (req_id, tc, "cc", cc), (req_id, tc, "ar", ar)]
        )
        rows["res_toolchain_runs_on"].append((req_id, tc, host))
    return rows


def os_release_families(path: str = "/etc/os-release") -> list[str]:
    try:
        text = Path(path).read_text()
    except OSError:
        return []
    vals: dict[str, str] = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            vals[k.strip()] = v.strip().strip('"')
    return sorted({vals.get("ID", ""), *vals.get("ID_LIKE", "").split()} - {""})


def inventory_rows(
    results: dict[str, set[tuple[str, ...]]], host: str, cross: str
) -> set[tuple[str, ...]]:
    """The rows of relation `host` (the host inventory's) and of every
    cross-toolchain inventory's `res_cross_<name>_<cross>` (a plugin for an
    SDK, e.g. the NDK or Emscripten, states its own platforms and toolchains
    this way): one inventory to the capability probes."""
    pat = re.compile(rf"res_cross_[a-z0-9]+_{cross}\Z")
    rows = set(results.get(host, set()))
    for rel, found in results.items():
        if pat.match(rel):
            rows |= found
    return rows


def tool_index(results: dict[str, set[tuple[str, ...]]]) -> dict[str, dict[str, str]]:
    tools: dict[str, dict[str, str]] = {}
    for _req, tc, role, path in inventory_rows(results, "res_toolchain_tool", "toolchain_tool"):
        tools.setdefault(tc, {})[role] = path
    versions = {
        tc: ver for _req, tc, _fam, ver in inventory_rows(results, "res_toolchain", "toolchain")
    }
    for tc, ver in versions.items():
        tools.setdefault(tc, {})["version"] = ver
    return tools


def platform_triples(results: dict[str, set[tuple[str, ...]]]) -> dict[str, str]:
    return {p: tr for _req, p, tr in inventory_rows(results, "res_platform", "platform")}


def platform_sdks(results: dict[str, set[tuple[str, ...]]]) -> dict[str, str]:
    return {p: sdk for _req, p, sdk in results.get("res_platform_sdk", set())}


def tool_families(results: dict[str, set[tuple[str, ...]]]) -> dict[str, str]:
    return {
        tc: fam for _req, tc, fam, _ver in inventory_rows(results, "res_toolchain", "toolchain")
    }


def cross_targets(results: dict[str, set[tuple[str, ...]]]) -> dict[str, set[str]]:
    """The platforms each cross toolchain (the NDK, Emscripten) is for."""
    out: dict[str, set[str]] = {}
    for _req, tc, p in inventory_rows(results, "", "toolchain_target"):
        out.setdefault(tc, set()).add(p)
    return out


def target_args(results: dict[str, set[tuple[str, ...]]], tc: str, p: str | None) -> list[str]:
    """How a probe aims tc at platform p: clang's --target (and the SDK);
    a GCC targets only the triple it was built for (gcc_targets), so it
    takes nothing. p None: the first SDK, for probes not about a platform;
    an SDK toolchain's probes aim at its first platform and take no macOS SDK."""
    own = cross_targets(results).get(tc)
    sdks = platform_sdks(results)
    if own and p is None:
        p = min(own)
    sdk = (
        sdks.get(p) if p is not None else (None if own else next(iter(sorted(sdks.values())), None))
    )
    args: list[str] = []
    probe = {(t, q): tr for _r, t, q, tr in inventory_rows(results, "", "toolchain_probe_triple")}
    if p is not None and tool_families(results).get(tc) == "clang":
        args.append(f"--target={probe.get((tc, p), platform_triples(results)[p])}")
    if sdk:
        args += ["-isysroot", sdk]
    return args


@functools.cache
def gcc_machine(cc: str) -> str:
    return which([cc, "-dumpmachine"]) or ""


def gcc_targets(tools: dict[str, str], triple: str) -> bool:
    return gcc_machine(tools["cc"]) == triple


def cc_capability(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    tc, lang, p = args
    tools = tool_index(results)
    cxx = tools[tc]["cxx"]
    cc = tools[tc]["cc"]
    common = target_args(results, tc, p)
    if tool_families(results).get(tc) == "gcc" and not gcc_targets(
        tools[tc], platform_triples(results)[p]
    ):
        return {"res_cc_capability": [(req_id, tc, lang, p, "no")]}
    if lang == "cxx":
        ok = compile_probe(ctx, [cxx] + common + ["-c"], "int f(){return 0;}\n", ".cpp", req_id)
    elif lang == "c":
        ok = compile_probe(ctx, [cc] + common + ["-c"], "int f(void){return 0;}\n", ".c", req_id)
    elif lang == "native-link":
        ok = compile_probe(ctx, [cxx] + common, "int main(){return 0;}\n", ".cpp", req_id)
    else:
        ok = False
    return {"res_cc_capability": [(req_id, tc, lang, p, "yes" if ok else "no")]}


def cxx_std(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    tc, std = args
    tools = tool_index(results)
    ok = compile_probe(
        ctx,
        [tools[tc]["cxx"], *target_args(results, tc, None), f"-std=c++{std}", "-c"],
        "int f(){return 0;}\n",
        ".cpp",
        req_id,
    )
    return {"res_cxx_std": [(req_id, tc, std, "yes" if ok else "no")]}


def cxx_modules(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    """Can the toolchain scan (P1689) and build named modules? clang: the
    clang-scan-deps beside clang++ must report a provided module and clang
    must build and use a BMI (Apple clang ships neither, so it says no).
    GCC: the driver must accept -fdeps-format=p1689r5 (GCC 14) and a module
    map. The answer is the scanner's path and yes | no."""
    (tc,) = args
    tools = tool_index(results)
    cxx = tools[tc]["cxx"]
    family = tool_families(results).get(tc)
    d = ctx.workdir / safe_dir(req_id)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)

    def run(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, cwd=d, check=False)

    (d / "m.cppm").write_text("export module m;\nexport int f() { return 1; }\n")
    (d / "u.cpp").write_text("import m;\nint main() { return f() - 1; }\n")
    common = [*target_args(results, tc, None), "-std=c++20"]
    ok, scanner = False, cxx
    try:
        if family == "clang":
            scanner = os.path.join(os.path.dirname(cxx), "clang-scan-deps")
            if os.access(scanner, os.X_OK):
                r = run([scanner, "-format=p1689", "--", cxx, *common, "-c", "m.cppm", "-o", "m.o"])
                provides = (
                    json.loads(r.stdout)["rules"][0].get("provides") if r.returncode == 0 else None
                )
                if provides and provides[0]["logical-name"] == "m":
                    ok = (
                        run(
                            [
                                cxx,
                                *common,
                                "-x",
                                "c++-module",
                                "--precompile",
                                "m.cppm",
                                "-o",
                                "m.pcm",
                            ]
                        ).returncode
                        == 0
                        and run(
                            [cxx, *common, "-fmodule-file=m=m.pcm", "-c", "u.cpp", "-o", "u.o"]
                        ).returncode
                        == 0
                    )
        elif family == "gcc":
            r = run(
                [
                    cxx,
                    *common,
                    "-fmodules-ts",
                    "-x",
                    "c++",
                    "-E",
                    "-fdeps-format=p1689r5",
                    "-fdeps-file=m.ddi",
                    "-fdeps-target=m.o",
                    "m.cppm",
                    "-o",
                    "m.ii",
                    "-MT",
                    "m.ddi",
                    "-MD",
                    "-MF",
                    "m.ddi.d",
                ]
            )
            if r.returncode == 0 and (d / "m.ddi").is_file():
                ok = (
                    json.loads((d / "m.ddi").read_text())["rules"][0]["provides"][0]["logical-name"]
                    == "m"
                )
    except (OSError, ValueError, KeyError, IndexError):
        ok = False
    shutil.rmtree(d, ignore_errors=True)
    return {"res_cxx_modules": [(req_id, tc, scanner, "yes" if ok else "no")]}


def cxx_std_module(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    """The source of the standard library's `std` module: libc++ lists it in
    libc++.modules.json (beside the library); a toolchain without that file
    (GCC 14's libstdc++) has none. Answer: source path, include dir, yes | no."""
    (tc,) = args
    cxx = tool_index(results)[tc]["cxx"]
    no = {"res_cxx_std_module": [(req_id, tc, "", "", "no")]}
    names = ("libc++.modules.json",)
    cands = [Path(cxx).resolve().parent.parent / "lib" / "c++" / n for n in names]
    r = subprocess.run(
        [cxx, "-print-file-name=libc++.modules.json"], capture_output=True, text=True, check=False
    )
    if r.returncode == 0 and os.path.isabs(r.stdout.strip()):
        cands.insert(0, Path(r.stdout.strip()))
    for meta in cands:
        try:
            data = json.loads(meta.read_text())
            std = next(m for m in data["modules"] if m["logical-name"] == "std")
            src = (meta.parent / std["source-path"]).resolve()
            incs = std.get("local-arguments", {}).get("system-include-directories", [])
            inc = (meta.parent / incs[0]).resolve() if incs else src.parent
        except (OSError, ValueError, KeyError, StopIteration):
            continue
        if src.is_file():
            return {"res_cxx_std_module": [(req_id, tc, str(src), str(inc), "yes")]}
    return no


def cc_warning(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    """Does the toolchain accept warning flag `flag` for `lang`? clang only
    warns about an unknown warning option, so the probe makes that an error
    (a probe argument, not a build flag)."""
    tc, lang, flag = args
    tools = tool_index(results)
    driver, src, suffix = (
        (tools[tc]["cxx"], "int f(){return 0;}\n", ".cpp")
        if lang == "cxx"
        else (tools[tc]["cc"], "int f(void){return 0;}\n", ".c")
    )
    # GCC rejects an unknown -W option by itself (but not an unknown -Wno-).
    strict = [] if tool_families(results).get(tc) == "gcc" else ["-Werror=unknown-warning-option"]
    argv = [driver, *target_args(results, tc, None), *strict, flag, "-c"]
    ok = compile_probe(ctx, argv, src, suffix, req_id)
    return {"res_cc_warning": [(req_id, tc, lang, flag, "yes" if ok else "no")]}


def cc_implicit_include(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    """The dirs the toolchain searches for <...> includes in `lang` without
    being told (`-v` prints them). Naming one again as -isystem moves it
    ahead of the compiler's own order and breaks #include_next in libstdc++
    (GCC: <cstdlib> then finds no <stdlib.h>), so a compile drops them, as
    CMake drops its implicit include directories. "" stands for none."""
    tc, lang = args
    tools = tool_index(results)
    driver, x = (tools[tc]["cxx"], "c++") if lang == "cxx" else (tools[tc]["cc"], "c")
    argv = [driver, *target_args(results, tc, None), "-E", "-x", x, "-v", os.devnull]
    r = subprocess.run(argv, capture_output=True, text=True, check=False)
    dirs, inside = [], False
    for line in r.stderr.splitlines():
        if line.startswith("#include <...> search starts here:"):
            inside = True
        elif line.startswith("End of search list."):
            inside = False
        elif inside:
            d = line.strip().removesuffix(" (framework directory)")
            dirs.append(os.path.normpath(d))
    return {"res_cc_implicit_include": [(req_id, tc, lang, d) for d in sorted(set(dirs)) or [""]]}


def toolchain_fingerprint(
    args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]], *, toolchain_arg: int
) -> str:
    """Cache fingerprint: the tool paths of the toolchain named by
    args[toolchain_arg], each tool's file identity (so an in-place update of a
    binary invalidates), and the environment the compiler reads."""
    tools = tool_index(results).get(args[toolchain_arg], {})
    ident = {
        "tools": tools,
        "identity": {r: file_identity(t) for r, t in sorted(tools.items()) if r != "version"},
        "env": {k: os.environ.get(k) for k in CHECK_ENV},
    }
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()


# Environment variables `inventory` reads, directly or through xcrun/which.
INVENTORY_ENV = (
    "PATH",
    "CC",
    "CXX",
    "SDKROOT",
    "DEVELOPER_DIR",
    "TOOLCHAINS",
    "BUILDLOG_LLVM_BIN",
    "BUILDLOG_LINUX_TOOLCHAINS",
)
CLT_DIR = "/Library/Developer/CommandLineTools"
XCODE_SELECT_LINK = "/var/db/xcode_select_link"


def _developer_dirs() -> list[str]:
    """The developer directories xcrun may resolve tools and SDKs in: the
    DEVELOPER_DIR override, the xcode-select choice, the Command Line Tools."""
    dirs = []
    if os.environ.get("DEVELOPER_DIR"):
        dirs.append(os.environ["DEVELOPER_DIR"])
    try:
        dirs.append(os.readlink(XCODE_SELECT_LINK))
    except OSError:
        pass
    dirs.append(CLT_DIR)
    return dirs


def inventory_fingerprint(args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]) -> str:
    """Cache fingerprint of `inventory`: everything it reads, by stat alone.

    Environment: INVENTORY_ENV. Linux: the identity of every tool it finds on
    PATH and of /etc/os-release. macOS: xcrun's output cannot be fingerprinted
    cheaply, so the inputs that determine it are: the developer directories
    (DEVELOPER_DIR, the xcode-select link, the Command Line Tools) with their
    version.plist, the tools and the SDK (and its SDKSettings.json) found
    under each, and xcrun itself. The upstream LLVM bin tools by identity.
    Not covered: a changed xcode-select choice that leaves the link target
    path and every file above untouched (not possible in practice)."""
    paths: dict[str, list] = {}

    def add(p: str | None) -> None:
        if p:
            paths[p] = file_identity(p)

    if sys.platform == "linux":
        for name in ("g++", "gcc", "gcc-ar", "ar", "clang++", "clang", "llvm-ar"):
            add(shutil.which(name))
        add("/etc/os-release")
    else:
        add(shutil.which("xcrun"))
        for d in _developer_dirs():
            add(d)
            add(os.path.join(d, "Contents", "version.plist"))
            for bindir in (
                os.path.join(d, "Toolchains", "XcodeDefault.xctoolchain", "usr", "bin"),
                os.path.join(d, "usr", "bin"),
            ):
                for name in ("clang++", "clang", "ar", "lipo"):
                    add(os.path.join(bindir, name))
            for sdk in (
                os.path.join(d, "Platforms", "MacOSX.platform", "Developer", "SDKs", "MacOSX.sdk"),
                os.path.join(d, "SDKs", "MacOSX.sdk"),
            ):
                add(sdk)
                add(os.path.join(sdk, "SDKSettings.json"))
    llvm = os.environ.get("BUILDLOG_LLVM_BIN")
    if llvm:
        for name in ("clang++", "clang", "llvm-ar"):
            add(os.path.join(llvm, name))
    ident = {
        "machine": pyplatform.machine(),
        "env": {k: os.environ.get(k) for k in INVENTORY_ENV},
        "files": paths,
    }
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()


# ---- configure-time checks (zlib case study) --------------------------------
# A check compiles and links one small program with the toolchain's driver
# for a platform, as CMake's check_* modules do (try_compile builds an
# executable). Kinds: include (the prelude, then #include <subject>), type
# (sizeof(subject) after the prelude; its size is read back from the linked
# binary, CMake's INFO:size[] technique, so no program runs), function (a
# prototype-free call of subject, linked; no prelude, CMake's
# CheckFunctionExists), compiles (the prelude, then subject as the program
# text) and runs (compiles, then the program runs on this machine and must
# exit 0: check_cxx_source_runs; it asks what the machine can do, so a
# target platform this machine does not run answers no, and the machine's
# CPU is part of the fingerprint). The request states everything else the answer depends on:
# the prelude headers and extra arguments; the fingerprint covers the tools
# and the SDK (cc_check_fingerprint), and the answer's watch set the files
# the compile read and the directories it searched (check_watch).

# Bump when a check's program text or command changes: cached answers of
# the old implementation are then not reused.
CHECK_VERSION = "1"


def check_program(kind: str, subject: str, headers: list[str], cxx: bool) -> str:
    prelude = "".join(f"#include <{h}>\n" for h in headers)
    if kind == "include":
        return f"{prelude}#include <{subject}>\n\nint main(void) {{ return 0; }}\n"
    if kind == "type":
        digits = ", ".join(f"('0' + ((SIZE / {10**k}) % 10))" for k in range(5, -1, -1))
        return (
            f"{prelude}\n#define SIZE (sizeof({subject}))\n"
            "static char info_size[] = {'I', 'N', 'F', 'O', ':', 's', 'i', 'z', 'e', '[',\n"
            f"  {digits}, ']', '\\0'}};\n"
            "int main(int argc, char *argv[]) { (void)argv; return info_size[argc]; }\n"
        )
    if kind == "function":
        linkage = 'extern "C" ' if cxx else ""
        return f"{linkage}char {subject}(void);\n\nint main(void) {{ return (int){subject}(); }}\n"
    if kind in ("compiles", "runs"):
        return f"{prelude}{subject}\n"
    raise ValueError(f"unknown check kind {kind}")


def _runs_ok(exe: Path) -> bool:
    try:
        ran = subprocess.run(
            [str(exe)], capture_output=True, cwd=exe.parent, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return ran.returncode == 0


_SEARCH_START = ("#include <...> search starts here:", '#include "..." search starts here:')


def _depfile_paths(depfile: Path) -> list[str]:
    """The prerequisites of a make-format depfile (backslash-escaped spaces,
    line continuations); empty when the compiler wrote none."""
    try:
        text = depfile.read_text()
    except OSError:
        return []
    text = text.replace("\\\n", " ")
    words = re.split(r"(?<!\\)\s+", text.split(": ", 1)[1] if ": " in text else "")
    return [w.replace("\\ ", " ") for w in words if w]


def _search_dirs(verbose: str) -> tuple[list[str], list[str], list[str]]:
    """From `-v` output: the directories searched for headers (including
    framework directories and those the driver reports as missing) and the
    `-L` directories of the linker command."""
    present: list[str] = []
    missing = re.findall(r'ignoring nonexistent directory "([^"]*)"', verbose)
    libs: list[str] = []
    inside = False
    for line in verbose.splitlines():
        if line.startswith(_SEARCH_START):
            inside = True
        elif line.startswith("End of search list"):
            inside = False
        elif inside and line.startswith(" "):
            present.append(line.strip().removesuffix(" (framework directory)"))
        elif line.startswith('"') and ' "-L' in line:
            try:
                libs += [a[2:] for a in shlex.split(line) if a.startswith("-L") and len(a) > 2]
            except ValueError:
                pass
    return present, missing, libs


def check_watch(depfile: Path, verbose: str, argv: list[str], workdir: Path) -> list[list]:
    """The watch set of a check (discovery.WATCH_KEY): the files its compile
    read (the depfile), and the directories whose entries decide what a
    header or library name resolves to: every header search directory
    (present, framework or missing) and the linker's -L directories, so that
    a header or library added, removed or replaced there is noticed. Library
    files named by `-l` in the extra arguments are watched as files."""
    present, missing, libdirs = _search_dirs(verbose)
    work = str(workdir.resolve())

    def outside(path: str) -> bool:
        full = os.path.realpath(os.path.join(workdir, path))
        return full != work and not full.startswith(work + os.sep)

    def absolute(paths: list[str]) -> set[str]:
        return {os.path.join(workdir, p) for p in paths if outside(p)}

    dirs = sorted(absolute([*present, *missing, *libdirs]))
    files = absolute(_depfile_paths(depfile))
    for a in argv:
        if a.startswith("-l") and len(a) > 2:
            for dd in libdirs:
                for ext in (".dylib", ".tbd", ".a", ".so"):
                    cand = os.path.join(dd, f"lib{a[2:]}{ext}")
                    if os.path.exists(cand):
                        files.add(cand)
        elif os.path.isfile(a):
            files.add(a)
    return [
        *(watch_identity(f) for f in sorted(files)),
        *(watch_identity(x, directory=os.path.isdir(x)) for x in dirs),
    ]


def cc_check(
    ctx, req_id: str, args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]
) -> dict[str, list[tuple[str, ...]]]:
    tc, _exec, p, lang, kind, subject, headers, extra = args
    tools = tool_index(results)[tc]
    cxx = lang == "cxx"
    driver = tools["cxx" if cxx else "cc"]
    argv = [driver, *target_args(results, tc, p), *extra.split()]
    src = check_program(kind, text_lines(subject), headers.split(), cxx)
    d = ctx.workdir / "cc-check" / safe_dir(req_id)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    probe = d / ("check.cpp" if cxx else "check.c")
    probe.write_text(src)
    depfile = d / "check.d"
    r = subprocess.run(
        [*argv, "-v", "-MD", "-MF", str(depfile), str(probe), "-o", str(d / "check")],
        capture_output=True,
        text=True,
        cwd=d,
        check=False,
    )
    # The program and the compiler's output stay for inspection.
    (d / "command.txt").write_text(" ".join(argv) + f" {probe.name} -o check\n")
    (d / "output.txt").write_text(r.stdout + r.stderr)
    ok = r.returncode == 0
    if ok and kind == "runs":
        ok = p == host_platform() and _runs_ok(d / "check")
    rows: dict[str, list[tuple[str, ...]]] = {"res_cc_check": [(req_id, "yes" if ok else "no")]}
    rows["res_cc_check_size"] = []
    rows[WATCH_KEY] = [
        *check_watch(depfile, r.stderr, argv, d),
    ]
    if ok and kind == "type":
        m = re.search(rb"INFO:size\[(\d+)\]", (d / "check").read_bytes())
        if not m:
            raise RuntimeError(f"cc_check {req_id}: size marker missing from the linked program")
        rows["res_cc_check_size"].append((req_id, str(int(m.group(1)))))
    return rows


# Environment variables clang and ld64 read that add search paths or change
# the target (CPATH & co., SDKROOT, the deployment target): part of the key.
CHECK_ENV = (
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "LIBRARY_PATH",
    "SDKROOT",
    "MACOSX_DEPLOYMENT_TARGET",
    "CCC_OVERRIDE_OPTIONS",
)


def file_identity(path: str) -> list:
    """What a file's content is identified by, cheaply: where it resolves to,
    its size, modification time, inode and device (an Xcode or SDK update
    rewrites or replaces them)."""
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
        return [real, st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev]
    except OSError:
        return [path, None, None, None, None]


@functools.cache
def _linker(driver: str) -> str:
    return which([driver, "-print-prog-name=ld"]) or "ld"


def machine_identity() -> dict:
    """What a runs check's answer depends on besides the tools: the CPU."""
    if sys.platform == "darwin":
        out = which(["sysctl", "-n", "machdep.cpu.brand_string"]) or ""
        return {"cpu": out, "optional": which(["sysctl", "hw.optional"]) or ""}
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return {"cpu": pyplatform.processor()}
    keep = ("model name", "Features", "flags", "CPU implementer", "CPU part")
    lines = sorted({ln for ln in text.splitlines() if ln.split(":")[0].strip() in keep})
    return {"cpu": lines}


def cc_check_fingerprint(args: tuple[str, ...], results: dict[str, set[tuple[str, ...]]]) -> str:
    """Cache key part of a check: the check implementation, the toolchain's
    identity (every tool, its version, the linker the driver runs), the
    platform's triple and SDK, and the environment the tools read. The
    request's own arguments (toolchain, platforms, language, kind, subject,
    prelude, extra arguments) are the rest of the key (discovery.cache_key)."""
    tc, _exec, p = args[0], args[1], args[2]
    tools = tool_index(results).get(tc, {})
    sdk = platform_sdks(results).get(p, "")
    ident = {
        "check": CHECK_VERSION,
        "tools": {r: file_identity(t) for r, t in sorted(tools.items()) if r != "version"},
        "version": tools.get("version"),
        "linker": file_identity(_linker(tools["cc"])) if "cc" in tools else None,
        "triple": platform_triples(results).get(p),
        "sdk": file_identity(sdk),
        "sdk_settings": file_identity(os.path.join(sdk, "SDKSettings.json")),
        "env": {k: os.environ.get(k) for k in CHECK_ENV},
    }
    if args[4] == "runs":
        ident["machine"] = machine_identity()
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()
