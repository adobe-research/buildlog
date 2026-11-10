#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Runs its argv in the 7b image (an arm64 Linux container) with the case-sensitive volume
# mounted at its own path and the working directory kept, so that a build tree on the volume
# runs as it is. `platform_runs_on` names this script for the arm64-linux platform
# (libhalide/presets/cross-linux-aarch64.toml); HL_* variables (Halide's) are passed on.
# BUILDLOG_XVOL_MOUNT=/some/path mounts the volume there instead (and rewrites the volume's
# path in the working directory, the arguments and HL_*): a build that embeds its host path
# in a binary then fails, which is how the sysroot-free rpath is verified.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XVOL="$(cd "${BUILDLOG_XVOL:-$HERE/../xvol}" && pwd)"
IMAGE="${IMAGE:-buildlog-halide-linux:22}"
MOUNT="${BUILDLOG_XVOL_MOUNT:-$XVOL}"
env_args=()
while IFS= read -r name; do
    env_args+=(-e "$name=${!name//"$XVOL"/$MOUNT}")
done < <(compgen -e | grep '^HL_' || true)
args=()
for a in "$@"; do args+=("${a//"$XVOL"/$MOUNT}"); done
exec docker run --rm --platform linux/arm64 -v "$XVOL:$MOUNT" -w "${PWD//"$XVOL"/$MOUNT}" ${env_args[@]+"${env_args[@]}"} "$IMAGE" "${args[@]}"
