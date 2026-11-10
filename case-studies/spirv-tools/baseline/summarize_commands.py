# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Summarize `ninja -t commands` (normalized) for the in-scope targets.

Usage: python3 summarize_commands.py < commands-full.txt > commands.txt

Per target: the number of compiles, the first compile command (the flags are
the same for all of a target's sources) and the archive or link command;
then every generator command (Python over SPIRV-Headers' grammar).
"""

import re
import sys

TARGETS = {
    "SPIRV-Tools-static": ("source/CMakeFiles/SPIRV-Tools-static.dir/", "source/libSPIRV-Tools.a"),
    "SPIRV-Tools-shared": (
        "source/CMakeFiles/SPIRV-Tools-shared.dir/",
        "source/libSPIRV-Tools-shared.dylib",
    ),
    "SPIRV-Tools-opt": (
        "source/opt/CMakeFiles/SPIRV-Tools-opt.dir/",
        "source/opt/libSPIRV-Tools-opt.a",
    ),
}
for t in ("spirv-as", "spirv-dis", "spirv-val", "spirv-opt"):
    TARGETS[t] = (f"tools/CMakeFiles/{t}.dir/", f"tools/{t}")
for t in ("spirv_unit_tests", "c_interface", "c_interface_shared", "cpp_interface"):
    TARGETS[f"test_{t}"] = (f"test/CMakeFiles/test_{t}.dir/", f"test/test_{t}")

lines = sys.stdin.read().splitlines()
for name, (objdir, out) in TARGETS.items():
    compiles = [ln for ln in lines if f"-o {objdir}" in ln and " -c " in ln]
    link = [ln for ln in lines if re.search(rf"(-o |qc |cr ){re.escape(out)}( |$)", ln)]
    print(f"===== {name}: {len(compiles)} compiles")
    if compiles:
        print(compiles[0])
    for ln in link:
        print(ln)
print("===== generators")
for ln in lines:
    if "python" in ln.lower():
        print(ln)
