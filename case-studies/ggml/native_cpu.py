# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The build machine's CPU as ggml's CMake reads it (ARM_NATIVE_FLAG in
src/ggml-cpu/CMakeLists.txt): from `$CC -mcpu=native -E -v -`, the -march=
list, else the -mcpu= value, unless it is `native` itself. Prints a user file
that states it as GGML_NATIVE_CPU (nothing to state: the default is
-mcpu=native). buildlog runs nothing at configure time, so this is the
user's machine fact.

    native_cpu.py CC > native-cpu.toml
"""

import re
import subprocess
import sys


def native_flag(cc: str) -> str:
    r = subprocess.run(
        [cc, "-mcpu=native", "-E", "-v", "-"], input="", capture_output=True, text=True, check=False
    )
    if r.returncode == 0:
        march = re.search(r"-march=[^ ']+", r.stderr)
        mcpu = re.search(r"-mcpu=[^ ']+", r.stderr)
        if march and march.group() != "-march=native":
            return march.group()
        if mcpu and mcpu.group() != "-mcpu=native":
            return mcpu.group()
    return "-mcpu=native"


if __name__ == "__main__":
    flag = native_flag(sys.argv[1])
    print(f'option_value = [["GGML_NATIVE_CPU", "{flag}"]]')
