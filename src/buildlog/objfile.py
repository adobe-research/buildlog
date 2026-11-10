# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Architectures of a prebuilt native file, read from its headers in-process
(core L3's `file_archs` probe; replaces
buildlog-cpp's per-import `lipo -info` probe).

Recognized: Mach-O (thin, 32/64-bit, either byte order) and fat/universal
files (32- and 64-bit fat headers); ELF; COFF objects, PE images and COFF
short import members; Apple's bitcode wrapper (LTO objects); `ar` archives (BSD, GNU and COFF `.lib` variants),
whose architectures are the union over their object members. Anything else
(text stubs such as `.tbd`, scripts, raw bitcode) has no architectures, so it
covers no platform, as `lipo` reports for it. One known difference from
`lipo`: a bitcode wrapper with CPU_TYPE_ANY (some LTO archives) has none
here, where `lipo` asks LLVM for the module's triple.

Two ELF cases have no header of their own. A GNU ld script (glibc's
`libm.so`: `GROUP ( /lib/.../libm.so.6 AS_NEEDED ( ... ) )`) has the
architectures of the files it names, those that exist. An archive without
object members (glibc 2.34+'s `libdl.a`, `libpthread.a`) links nothing, so
it covers every architecture.

Architecture names are the ones `lipo` and the platform inventory use:
`arm64`, `arm64e`, `x86_64`, `x86_64h`, `i386`, `arm`, `arm64_32`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from pathlib import Path

from buildlog.providers import under_sysroot

# Bump when the reader's output changes, to invalidate cached results.
READER_VERSION = "2"

CPU_ARCH_ABI64 = 0x01000000
CPU_ARCH_ABI64_32 = 0x02000000
CPU_TYPE_X86 = 7
CPU_TYPE_ARM = 12
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce": ">",
    b"\xfe\xed\xfa\xcf": ">",
    b"\xce\xfa\xed\xfe": "<",
    b"\xcf\xfa\xed\xfe": "<",
}
BITCODE_WRAPPER = b"\xde\xc0\x17\x0b"
FAT_MAGIC = b"\xca\xfe\xba\xbe"
FAT_MAGIC_64 = b"\xca\xfe\xba\xbf"
# A fat header's arch count; Java class files share the magic with a
# version number (>= 45) in its place.
FAT_MAX_ARCHS = 30
ELF_MACHINES = {3: "i386", 40: "arm", 62: "x86_64", 183: "arm64"}
COFF_MACHINES = {0x14C: "i386", 0x1C0: "arm", 0x1C4: "arm", 0x8664: "x86_64", 0xAA64: "arm64"}
AR_MAGIC = b"!<arch>\n"
AR_HEADER = 60
# Symbol tables and name tables of the ar variants, not objects.
AR_INDEX_NAMES = {b"/", b"//", b"/SYM64/", b"__.SYMDEF", b"__.SYMDEF SORTED", b"__.SYMDEF_64"}
HEAD = 64
ALL_ARCHS = ("arm64", "arm64e", "x86_64", "x86_64h", "i386", "arm", "arm64_32")


def macho_arch(cputype: int, subtype: int) -> str | None:
    sub = subtype & 0x00FFFFFF
    if cputype == CPU_TYPE_X86 | CPU_ARCH_ABI64:
        return "x86_64h" if sub == 8 else "x86_64"
    if cputype == CPU_TYPE_ARM | CPU_ARCH_ABI64:
        return "arm64e" if sub == 2 else "arm64"
    if cputype == CPU_TYPE_ARM | CPU_ARCH_ABI64_32:
        return "arm64_32"
    if cputype == CPU_TYPE_X86:
        return "i386"
    if cputype == CPU_TYPE_ARM:
        return "arm"
    return None


def _object_archs(head: bytes) -> tuple[str, set[str]] | None:
    """(format, archs) of an object or image from its first bytes, or None."""
    if head[:4] in MACHO_MAGICS and len(head) >= 12:
        cputype, subtype = struct.unpack(MACHO_MAGICS[head[:4]] + "II", head[4:12])
        arch = macho_arch(cputype, subtype)
        return "macho", {arch} if arch else set()
    if head[:4] == BITCODE_WRAPPER and len(head) >= 20:
        # Apple's bitcode wrapper (LTO objects): a Mach-O cputype at 16.
        (cputype,) = struct.unpack("<I", head[16:20])
        arch = macho_arch(cputype, 0)
        return "bitcode", {arch} if arch else set()
    if head[:4] == b"\x7fELF" and len(head) >= 20:
        order = {1: "<", 2: ">"}.get(head[5])
        if order is None:
            return None
        (machine,) = struct.unpack(order + "H", head[18:20])
        arch = ELF_MACHINES.get(machine)
        return "elf", {arch} if arch else set()
    if len(head) >= 8 and head[:4] == b"\x00\x00\xff\xff":
        # COFF short import member (import libraries): machine at 6.
        (machine,) = struct.unpack("<H", head[6:8])
        if machine in COFF_MACHINES:
            return "coff", {COFF_MACHINES[machine]}
    if len(head) >= 2:
        (machine,) = struct.unpack("<H", head[:2])
        if machine in COFF_MACHINES:
            return "coff", {COFF_MACHINES[machine]}
    return None


def _pe_archs(f, head: bytes) -> tuple[str, set[str]] | None:
    if head[:2] != b"MZ" or len(head) < 0x40:
        return None
    (offset,) = struct.unpack("<I", head[0x3C:0x40])
    f.seek(offset)
    sig = f.read(6)
    if len(sig) < 6 or sig[:4] != b"PE\x00\x00":
        return None
    (machine,) = struct.unpack("<H", sig[4:6])
    arch = COFF_MACHINES.get(machine)
    return "coff", {arch} if arch else set()


def _fat_archs(f, head: bytes) -> tuple[str, set[str]] | None:
    wide = head[:4] == FAT_MAGIC_64
    if head[:4] not in (FAT_MAGIC, FAT_MAGIC_64) or len(head) < 8:
        return None
    (n,) = struct.unpack(">I", head[4:8])
    if not 0 < n <= FAT_MAX_ARCHS:
        return None
    size = 32 if wide else 20
    f.seek(8)
    table = f.read(n * size)
    if len(table) < n * size:
        return None
    archs = set()
    for i in range(n):
        cputype, subtype = struct.unpack(">II", table[i * size : i * size + 8])
        arch = macho_arch(cputype, subtype)
        if arch:
            archs.add(arch)
    return "fat", archs


def _ar_archs(f, total: int) -> tuple[str, set[str]]:
    """The union of the archive's object members' architectures."""
    archs: set[str] = set()
    members = 0
    pos = len(AR_MAGIC)
    while pos + AR_HEADER <= total:
        f.seek(pos)
        header = f.read(AR_HEADER)
        if len(header) < AR_HEADER or header[58:60] != b"`\n":
            break
        name = header[:16].rstrip(b" ")
        try:
            size = int(header[48:58].strip() or b"0")
        except ValueError:
            break
        data = pos + AR_HEADER
        skip = 0
        if name.startswith(b"#1/"):
            # BSD: the member name follows the header, counted in size.
            try:
                skip = int(name[3:])
            except ValueError:
                break
            f.seek(data)
            name = f.read(skip).rstrip(b"\x00")
        if name.rstrip(b"/") and name not in AR_INDEX_NAMES and not name.startswith(b"__.SYMDEF"):
            members += 1
            f.seek(data + skip)
            got = _object_archs(f.read(HEAD))
            if got is not None:
                archs |= got[1]
        pos = data + size + (size & 1)
    if not archs and not members:
        return "ar", set(ALL_ARCHS)
    return "ar", archs


LD_SCRIPT = re.compile(rb"^\s*(?:/\*.*?\*/\s*)*(?:OUTPUT_FORMAT|GROUP|INPUT)\s*\(", re.DOTALL)
LD_PATH = re.compile(rb"(?<![\w.-])(/[^\s()]+)")


def _ld_script_archs(f, total: int, root: str) -> tuple[str, set[str]] | None:
    """A GNU ld script's architectures: those of the existing files it names."""
    f.seek(0)
    text = f.read(min(total, 4096))
    if not LD_SCRIPT.match(text) or b"\x00" in text:
        return None
    archs: set[str] = set()
    for named in LD_PATH.findall(text):
        # ld looks an absolute path of a script in the sysroot up under it.
        path = Path(under_sysroot(root, named.decode(errors="replace")))
        if path.is_file():
            archs |= file_archs_of(path, root)[1]
    return "ldscript", archs


def file_archs_of(path: Path, root: str = "") -> tuple[str, set[str]]:
    """(format, architectures) of one file; ("unknown", set()) otherwise."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD)
            if head.startswith(AR_MAGIC):
                return _ar_archs(f, os.fstat(f.fileno()).st_size)
            for reader in (_fat_archs, _pe_archs):
                got = reader(f, head)
                if got is not None:
                    return got
            got = _object_archs(head)
            if got is None and (script := _ld_script_archs(f, os.fstat(f.fileno()).st_size, root)):
                return script
            return got or ("unknown", set())
    except OSError:
        return "missing", set()


def file_archs(ctx, req_id: str, args: tuple[str, ...], results) -> dict[str, list]:
    file, root = args
    fmt, archs = file_archs_of(Path(file), root)
    return {
        "res_file_format": [(req_id, file, fmt)],
        "res_file_arch": [(req_id, file, a) for a in sorted(archs)],
        "__origin__": [
            ("res_file_format", (req_id, file, fmt), file),
            *(("res_file_arch", (req_id, file, a), file) for a in sorted(archs)),
        ],
    }


def file_fingerprint(args: tuple[str, ...], results) -> str:
    """The file's identity and stat: stale results cannot persist."""
    file, root = args
    try:
        st = os.stat(file)
        token = [st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev]
    except OSError:
        token = None
    data = [READER_VERSION, os.path.realpath(file), token, root]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()
