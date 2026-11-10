#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Build the Linux/GCC image for the Halide case study (M3 phases 7b and 7c).
#
#   build-image.sh [docker build args...]
#
# Environment overrides:
#   IMAGE   image tag (default buildlog-halide-linux:22)
#
# Records the resolved tool versions, the image id and its size in
# raw/image.txt (gitignored).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE:-buildlog-halide-linux:22}"
RAW="$HERE/raw"
mkdir -p "$RAW"

start=$(date +%s)
docker build --platform linux/arm64 -t "$IMAGE" "$@" "$HERE/linux" 2>&1 | tee "$RAW/image-build.log"
end=$(date +%s)

{
    echo "image=$IMAGE"
    echo "id=$(docker image inspect --format '{{.Id}}' "$IMAGE")"
    echo "size_bytes=$(docker image inspect --format '{{.Size}}' "$IMAGE")"
    echo "build_seconds=$((end - start))"
    docker run --rm "$IMAGE" cat /etc/halide-case-versions
} | tee "$RAW/image.txt"
