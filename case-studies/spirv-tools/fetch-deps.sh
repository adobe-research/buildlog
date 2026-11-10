#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Populates upstream/external/ with the dependencies SPIRV-Tools' tests build
# against (googletest, effcee, re2, abseil_cpp), at the revisions of the
# pinned SPIRV-Tools' DEPS file. SPIRV-Tools git-ignores external/, so the
# submodule stays clean. Not spirv-headers: this study imports an install.
set -euo pipefail
UP="$(cd "$(dirname "${BASH_SOURCE[0]}")/upstream" && pwd)"
fetch() { # DIR URL REV
    local dir="$UP/external/$1"
    if [[ -d "$dir/.git" || -f "$dir/.git" ]] && [[ "$(git -C "$dir" rev-parse HEAD)" == "$3" ]]; then
        return
    fi
    rm -rf "$dir"
    git init -q "$dir"
    git -C "$dir" fetch -q --depth 1 "$2" "$3"
    git -C "$dir" checkout -q FETCH_HEAD
}
fetch abseil_cpp https://github.com/abseil/abseil-cpp.git db1bdc86effd20c9ac9c9939326c0d280c9ff733
fetch effcee https://github.com/google/effcee.git 910ed15722d5d05c9d71ecf36c1a22243cb79b02
fetch googletest https://github.com/google/googletest.git 8240fa7d62f73e01c7af27d61ed965d6d66698fa
fetch re2 https://github.com/google/re2.git 972a15cedd008d846f1a39b2e88ce48d7f166cbd
