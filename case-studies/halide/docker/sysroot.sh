#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# M3 phase 7c: export the Linux/GCC image's filesystem as a sysroot for a
# macOS -> aarch64-linux-gnu cross build.
#
#   sysroot.sh [DEST]      (default: ./sysroot, gitignored)
#
# Environment overrides:
#   IMAGE   (default buildlog-halide-linux:22)
#
# The sysroot holds what a Linux libHalide and its tests compile and link against:
#   usr/include               glibc, libstdc++ (c++/13), png, jpeg, zlib, zstd, ...
#   usr/lib/aarch64-linux-gnu glibc, libstdc++, crt1.o etc., and the -dev .so links
#   usr/lib/gcc/aarch64-linux-gnu/13   crtbegin*.o, libgcc*, libstdc++ link scripts
#   usr/lib/llvm-22           Linux LLVM/Clang/LLD headers, libs and CMake packages
#   usr/local/{include,lib,bin}  flatbuffers 25.12.19 and wabt 1.0.42 (source-built)
#   usr/lib/cmake             other CMake packages (pybind11 etc.)
#   lib/, usr/lib/ld-linux-aarch64.so.1   /lib (see below) and the loader libc.so names
# Linux executables in it (usr/lib/llvm-22/bin/*, usr/local/bin/flatc, ...) are kept, so
# that the CMake packages that name them load, but they cannot run on macOS.
#
# Absolute symlinks are rewritten to relative ones inside the sysroot. Paths that differ
# only by case (a few Linux kernel headers) collide on a case-insensitive APFS volume;
# they are listed in raw/sysroot-case-collisions.txt.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE:-buildlog-halide-linux:22}"
DEST="${1:-$HERE/sysroot}"
RAW="$HERE/raw"
mkdir -p "$RAW"
log() { printf '== %s\n' "$*" >&2; }

MEMBERS=(
    usr/include
    usr/lib/aarch64-linux-gnu
    usr/lib/ld-linux-aarch64.so.1
    usr/lib/gcc
    usr/lib/llvm-22
    usr/lib/cmake
    usr/local/include
    usr/local/lib
    usr/local/bin
    usr/local/share
    lib
)

start=$(date +%s)
cid=$(docker create --platform linux/arm64 "$IMAGE" true)
trap 'docker rm -f "$cid" >/dev/null' EXIT
log "export $IMAGE ($cid) -> $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
docker export "$cid" >"$RAW/sysroot-export.tar"
tar -tf "$RAW/sysroot-export.tar" >"$RAW/sysroot-export.list"
# bsdtar exits non-zero when a hard link's target is outside MEMBERS; those are
# reported and then copied as files below.
set +e
tar -x -f "$RAW/sysroot-export.tar" -C "$DEST" "${MEMBERS[@]}" 2>"$RAW/sysroot-tar.log"
set -e

# Paths that a case-insensitive file system cannot hold side by side.
python3 - "$RAW/sysroot-export.list" "$RAW/sysroot-case-collisions.txt" "${MEMBERS[@]}" <<'EOF'
import collections, sys
listing, out, *members = sys.argv[1:]
seen = collections.defaultdict(set)
for line in open(listing):
    p = line.rstrip("\n").rstrip("/")
    if any(p == m or p.startswith(m + "/") for m in members):
        seen[p.lower()].add(p)
with open(out, "w") as f:
    for group in sorted(v for v in seen.values() if len(v) > 1):
        f.write("  ".join(sorted(group)) + "\n")
EOF

# Rewrite absolute symlinks as relative ones within the sysroot.
python3 - "$DEST" <<'EOF'
import os, sys
root = os.path.realpath(sys.argv[1])
fixed = 0
for dirpath, dirnames, filenames in os.walk(root):
    for name in dirnames + filenames:
        p = os.path.join(dirpath, name)
        if not os.path.islink(p):
            continue
        target = os.readlink(p)
        if os.path.isabs(target):
            new = os.path.relpath(root + target, dirpath)
            os.remove(p)
            os.symlink(new, p)
            fixed += 1
print(f"rewrote {fixed} absolute symlinks", file=sys.stderr)
EOF
rm -f "$RAW/sysroot-export.tar"

# Undo the usrmerge `lib -> usr/lib` link. Through it clang selects the GCC installation
# $DEST/lib/gcc/aarch64-linux-gnu/13 and adds $DEST/lib/gcc/.../13/../../../../include/c++/13,
# which the depfiles spell, collapsed lexically, as $DEST/include/c++/13/... (a path that
# does not exist), so ninja never becomes clean. A real lib/ directory with links to each
# usr/lib entry except gcc keeps /lib/... (named by libc.so's linker script) resolvable and
# makes clang select usr/lib/gcc.
if [[ -L "$DEST/lib" ]]; then
    rm "$DEST/lib"
    mkdir "$DEST/lib"
    for e in "$DEST"/usr/lib/*; do
        [[ "$(basename "$e")" == gcc ]] || ln -s "../usr/lib/$(basename "$e")" "$DEST/lib/"
    done
fi
end=$(date +%s)

{
    echo "image=$IMAGE"
    echo "image_id=$(docker image inspect --format '{{.Id}}' "$IMAGE")"
    echo "sysroot=$DEST"
    echo "size=$(du -sh "$DEST" | cut -f1)"
    echo "files=$(find "$DEST" -type f | wc -l | tr -d ' ')"
    echo "case_collisions=$(wc -l <"$RAW/sysroot-case-collisions.txt" | tr -d ' ')"
    echo "tar_warnings=$(grep -c . "$RAW/sysroot-tar.log" || true)"
    echo "seconds=$((end - start))"
} | tee "$RAW/sysroot.txt"
