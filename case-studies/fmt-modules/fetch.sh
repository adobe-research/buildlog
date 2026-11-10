#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Fetches {fmt} 11.2.0 into upstream/ (git-ignored).
set -euo pipefail
UP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/upstream"
REV=40626af88bd7df9a5fb80be7b25ac85b122d6c21
if [[ -d "$UP/.git" ]] && [[ "$(git -C "$UP" rev-parse HEAD)" == "$REV" ]]; then exit 0; fi
rm -rf "$UP"
git init -q "$UP"
git -C "$UP" fetch -q --depth 1 https://github.com/fmtlib/fmt.git "$REV"
git -C "$UP" checkout -q FETCH_HEAD
