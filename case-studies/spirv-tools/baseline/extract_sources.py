# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Print the C++ sources of the in-scope CMake targets as TOML lists, from the
baseline build's compile_commands.json (CMake's order), for ../project/sources.toml.

    python3 extract_sources.py BUILD SRC TARGET...
"""

import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

build, src = sys.argv[1], sys.argv[2].rstrip("/") + "/"
by: OrderedDict[str, list[str]] = OrderedDict()
for e in json.loads(Path(build, "compile_commands.json").read_text()):
    m = re.search(r"CMakeFiles/([^/]+)\.dir/", e["output"])
    by.setdefault(m.group(1), []).append(e["file"].removeprefix(src))

for target in sys.argv[3:]:
    print(f"# {target}: {len(by[target])} sources")
    print("[")
    for s in by[target]:
        print(f'  "{s}",')
    print("]")
