# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Oracle: buildlog's LLVM discovery agrees with the Option 1 CMake probe.

    uv run python case-studies/halide/phase1/oracle_check.py WORKDIR [--llvm 22] [--llvm 21]

For each LLVM major N it writes a small project into WORKDIR/llvmN (the
case-study packages, the tier-2 LLVM/Clang/LLD packages of
llvm-install/homebrew-{llvm,lld}-N (imports of llvm@N's own CMake exports plus
facts and rules), shared linkage,
Halide's eight backends, one consumer of halide-build::llvm), configures it, and
compares what buildlog derived with raw/option1-*/rows.json, the facts the
Option 1 probe got by running Halide's own FindHalide_LLVM.cmake under CMake:

  version        package_version(LLVM)                 halide_llvm.version
  components     Halide's backends, all built          halide_llvm.components
                 (package_attr llvm.built_target)
  linkage        package_linkage_selected(LLVM)        halide_llvm.shared_libs
  RTTI           LLVM::LLVM's cxx.rtti (package rules)     halide_llvm.rtti
  defines        what a consumer of halide-build::llvm  Halide_LLVM::Core cc.define
                 gets (use), minus Halide's WITH_<backend>
  include dirs   use(..., cc.system_include_dir)       Halide_LLVM::* cc.include_dir
  rpath flags    use(..., native.link_flag)            LLVM::LLVM/LLD::* link_flag
  locations      imported locations                    res_pkg_location
                 (Option 1's LLVM::clang is Clang::clang here)

Exit status 0 when every row agrees.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PACKAGES = HERE.parent / "packages"
ROWS = {22: HERE / "raw/option1-native-arm64/rows.json", 21: HERE / "raw/option1-llvm21/rows.json"}
BACKENDS = ["AArch64", "ARM", "Hexagon", "NVPTX", "PowerPC", "RISCV", "WebAssembly", "X86"]
HOMEBREW = "/opt/homebrew"
OVERLAYS = HERE.parent / "llvm-install"

PROJECT = """uses = ["buildlog-cpp", "halide-build"]
spec = ["spec.toml"]
"""
SPEC = """config_decl = [["release", "arm64-macos"]]
config_decl_setting = [["release", "native.opt", "release"]]
halide_build_llvm_backend = [{backends}]
target = [["consumer"]]
native_kind = [["consumer", "executable"]]
cxx_source = [["consumer", "main.cpp"]]
dep = [["consumer", "halide-build::llvm", "private"], ["consumer", "Clang::clang", "private"]]
request = [["consumer", "release"]]
"""
USER = """toolchain_pin = [["c", "arm64-macos", "arm64-macos", "apple-clang"], ["cxx", "arm64-macos", "arm64-macos", "apple-clang"], ["native-link", "arm64-macos", "arm64-macos", "apple-clang"]]
package_search_dir = [["buildlog", "{ov}/homebrew-llvm-{n}/share/buildlog"], ["buildlog", "{ov}/homebrew-lld-{n}/share/buildlog"]]
package_linkage = [["LLVM", "shared"]]
"""


def read_csv(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return [row for row in csv.reader(f, delimiter="\t")]


def configure(work: Path, n: int) -> Path:
    proj = work / f"llvm{n}"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "buildlog.toml").write_text(PROJECT)
    backends = ", ".join(f'["{b}", "WITH_{b.upper()}"]' for b in BACKENDS)
    (proj / "spec.toml").write_text(SPEC.format(backends=backends))
    (proj / "user.toml").write_text(USER.format(ov=OVERLAYS, n=n))
    (proj / "main.cpp").write_text("int main() { return 0; }\n")
    build = work / f"build-llvm{n}"
    subprocess.run(
        ["uv", "run", "--frozen", "buildlog", "configure", str(proj), "-C", str(build),
         "--package-path", str(PACKAGES), "--user", str(proj / "user.toml"), "--update-lock"],
        cwd=REPO, check=True, stdout=subprocess.DEVNULL,
    )  # fmt: skip
    return build


def real(path: str) -> str:
    return os.path.realpath(path)


def derived(build: Path) -> dict[str, set]:
    facts = build / "souffle" / "facts"
    use = [r for r in read_csv(facts / "use.csv") if r[0] == "consumer"]
    uses = lambda key: {r[3] for r in use if r[2] == key}
    version = {".".join(r[2:5]) for r in read_csv(facts / "package_version.csv") if r[0] == "LLVM"}
    built = {
        r[3]
        for r in read_csv(facts / "package_attr.csv")
        if r[0] == "LLVM" and r[2] == "llvm.built_target"
    }
    rtti = {
        r[3] for r in read_csv(facts / "use.csv") if r[0] == "LLVM::LLVM" and r[2] == "cxx.rtti"
    }
    locations = set()
    cache = json.loads((build / "probe-cache.json").read_text())
    for key, val in cache.items():
        if json.loads(key)[0] == "pkg_import":
            locations |= {(r[1], real(r[2])) for r in val.get("res_pkg_location", [])}
    # The overlay prefixes link lib/include/bin to the Homebrew kegs.
    rpath = "-Wl,-rpath,"
    return {
        "version": version,
        "components": set(BACKENDS) & built,
        "linkage": {
            r[2] for r in read_csv(facts / "package_linkage_selected.csv") if r[0] == "LLVM"
        },
        "rtti": rtti,
        "defines": {d for d in uses("cc.define") if not d.startswith("WITH_")},
        "include_dirs": {real(d) for d in uses("cc.system_include_dir")},
        "link_flags": {rpath + real(f[len(rpath) :]) for f in uses("native.link_flag")},
        "locations": locations,
    }


def oracle(rows_file: Path) -> dict[str, set]:
    rows = json.loads(rows_file.read_text())["rows"]
    setting = {r[1]: r[2] for r in rows["res_probe_setting"]}
    props = rows["res_pkg_prop"]
    rename = {"LLVM::clang": "Clang::clang"}
    return {
        "version": {setting["halide_llvm.version"]},
        "components": set(setting["halide_llvm.components"].split(";")),
        "linkage": {"shared" if setting["halide_llvm.shared_libs"] == "YES" else "static"},
        "rtti": {"on" if setting["halide_llvm.rtti"] == "ON" else "off"},
        "defines": {r[3] for r in props if r[2] == "cc.define"},
        "include_dirs": {r[3] for r in props if r[2] == "cc.include_dir"},
        "link_flags": {r[3] for r in props if r[2] == "native.link_flag"},
        "locations": {(rename.get(r[1], r[1]), r[2]) for r in rows["res_pkg_location"]},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workdir", type=Path)
    ap.add_argument("--llvm", type=int, action="append", choices=sorted(ROWS))
    args = ap.parse_args(argv)
    ok = True
    for n in args.llvm or [22]:
        if not os.path.isdir(f"{HOMEBREW}/opt/llvm@{n}"):
            print(f"llvm@{n}: not installed, skipped")
            continue
        got = derived(configure(args.workdir.resolve(), n))
        want = oracle(ROWS[n])
        for key, exp in want.items():
            have = got[key]
            if key == "locations":  # buildlog imports every target; compare Option 1's
                have = {loc for loc in have if loc[0] in {t for t, _ in exp}}
            same = have == exp
            ok &= same
            print(f"llvm@{n} {key:<13} {'agree' if same else 'DIFFER'}")
            if not same:
                print(
                    f"    only buildlog: {sorted(have - exp)}\n    only Option 1: {sorted(exp - have)}"
                )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
