#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Linux evaluation of buildlog in Docker (Ubuntu 24.04; GCC and Clang).
#
#   linux/run.sh            image, then the suite with gcc and clang, then the case studies
#   linux/run.sh image      build the image (CPU-limited: JOBS, default 4)
#   linux/run.sh suite gcc|clang [pytest args...]
#   linux/run.sh cases gcc|clang [STUDY...]   (all studies by default)
#   linux/run.sh exec gcc|clang "CMD"         a command in the ported copy
#   linux/run.sh baselines [gcc|clang] [STUDY...]
#                           upstream builds on Linux (CMake, Meson), written to the case
#                           studies' baseline/linux/ (gcc) or baseline/linux-clang/ directories
#   linux/run.sh shell      an interactive shell in the container
#
# The worktree is copied into the container (so Linux build dirs, venvs and
# locks never touch the macOS ones), and the copy is ported to the Linux
# host's platform and toolchain names (linux/port.py). The copy includes the
# case studies' source submodules (case-studies/*/upstream: run
# `git submodule update --init` first), except Halide's, which the Linux runs
# do not use. `image` needs SOUFFLE_SRC, a checkout of the patched Soufflé.
# Logs go to linux/raw/ (gitignored).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
IMAGE="${IMAGE:-buildlog-linux:24.04}"
SOUFFLE_SRC="${SOUFFLE_SRC:-}"
SOUFFLE_REF="${SOUFFLE_REF:-main}"
JOBS="${JOBS:-8}"
CPUS="${CPUS:-8}"
GITDIR="$(cd "$REPO" && git rev-parse --path-format=absolute --git-common-dir)"
RAW="$HERE/raw"
mkdir -p "$RAW"

image() {
    [[ -n "$SOUFFLE_SRC" ]] || { echo "run.sh image: set SOUFFLE_SRC to the patched Soufflé checkout" >&2; exit 2; }
    local ctx
    ctx="$(mktemp -d)"
    trap 'rm -rf "$ctx"' RETURN
    cp "$HERE/Dockerfile" "$ctx/"
    git -C "$SOUFFLE_SRC" archive --format=tar -o "$ctx/souffle.tar" "$SOUFFLE_REF"
    git -C "$SOUFFLE_SRC" rev-parse "$SOUFFLE_REF" | sed "s/^/souffle $SOUFFLE_REF /" > "$ctx/souffle-commit.txt"
    # Halide's pinned source (the submodule), as a tar: see the Dockerfile.
    git -C "$REPO/case-studies/halide/upstream" archive --format=tar -o "$ctx/halide-src.tar" HEAD
    docker build --build-arg "JOBS=$JOBS" -t "$IMAGE" "$ctx" 2>&1 | tee "$RAW/image-build.log"
    docker run --rm "$IMAGE" cat /etc/buildlog-versions | tee "$RAW/versions.txt"
}

# run <script>: the script runs in /work/buildlog, a copy of this worktree.
run() {
    docker run --rm -i --cpus "$CPUS" \
        -v "$GITDIR:/host-git:ro" \
        -e BUILDLOG_LINUX_TOOLCHAINS="${FAMILY:-gcc}" \
        -v "$REPO:/src:ro" -v "$RAW:/out" \
        "$IMAGE" bash -euo pipefail -c "
mkdir -p /work && rsync -a --exclude .venv --exclude buildlog-build --exclude 'linux/raw' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'case-studies/halide/upstream' \
  --exclude '.git/modules/case-studies/halide' /src/ /work/buildlog/
# A linked worktree's .git is a file naming a host path: drop it from the copy.
[ -f /work/buildlog/.git ] && rm -f /work/buildlog/.git
# The submodules' git dirs live in the host repository, outside the worktree
# copy: mount it, so the baseline scripts' pin checks and the generators that
# ask git (SPIRV-Tools' build-version.inc) see the checkouts.
mkdir -p /buildlog-wt && ln -sfn /work/buildlog /buildlog-wt/$(basename "$REPO")
for g in /work/buildlog/case-studies/*/upstream/.git; do
  [ -f \$g ] && sed -i 's#^gitdir: .*/\\.git/#gitdir: /host-git/#' \$g
done
cd /work/buildlog && uv sync --frozen -q && python3 linux/port.py ${FAMILY:-gcc}
$1"
}

suite() {
    FAMILY="$1"
    shift
    run "uv run --frozen pytest -q -n $CPUS -rs $* 2>&1" | tee "$RAW/suite-$FAMILY.log" | tail -60
}

cases() {
    FAMILY="$1"
    shift
    run "bash linux/cases.sh $*" 2>&1 | tee "$RAW/cases-$FAMILY.log"
}

# exec FAMILY CMD: an arbitrary command in the ported copy (development).
exec_in() {
    FAMILY="$1"
    run "$2" 2>&1
}

# Each case study's baseline script (linux/baselines.sh), run in the container;
# its Linux files (baseline/linux for gcc, baseline/linux-clang for clang,
# without raw/) are copied back into this worktree. zlib's is the GCC one only.
baselines() {
    local fam="${1:-gcc}"
    shift || true
    FAMILY="$fam"
    local dir=linux
    [[ "$fam" == clang ]] && dir=linux-clang
    run "bash linux/baselines.sh $*" 2>&1 | tee "$RAW/baselines-$fam.log"
    local s
    for s in "$RAW"/baselines/*-"$fam"; do
        [[ -d "$s" ]] || continue
        local study="$(basename "$s")"
        study="${study%-"$fam"}"
        local where="case-studies/$study"
        [[ "$study" == halide-apps ]] && where=case-studies/halide/apps
        mkdir -p "$REPO/$where/baseline/$dir"
        rsync -a "$s/" "$REPO/$where/baseline/$dir/"
    done
}

case "${1:-all}" in
image) image ;;
suite) shift; suite "$@" ;;
cases) shift; cases "$@" ;;
baselines) shift; baselines "$@" ;;
exec) shift; exec_in "$@" ;;
shell) docker run --rm -it --cpus "$CPUS" -v "$REPO:/src:ro" "$IMAGE" bash ;;
all)
    image
    for f in gcc clang; do suite "$f"; done
    for f in gcc clang; do cases "$f"; done
    ;;
*) echo "usage: $0 [image|suite FAMILY|cases FAMILY|shell|all]" >&2; exit 2 ;;
esac
