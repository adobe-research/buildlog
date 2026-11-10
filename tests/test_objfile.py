# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Core's file_archs probe reader (buildlog.objfile): object headers read
in-process, on synthetic fixtures of every recognized format, and against
`lipo -info` on files clang builds (macOS only)."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys

import pytest

from buildlog.objfile import file_archs, file_archs_of, file_fingerprint

ARM64 = (0x0100000C, 0)
ARM64E = (0x0100000C, 2)
X86_64 = (0x01000007, 3)


def macho(cpu, little=True, wide=True) -> bytes:
    magic = 0xFEEDFACF if wide else 0xFEEDFACE
    order = "<" if little else ">"
    return struct.pack(order + "III", magic, *cpu) + bytes(20)


def fat(*cpus, wide=False) -> bytes:
    out = struct.pack(">II", 0xCAFEBABF if wide else 0xCAFEBABE, len(cpus))
    for cpu in cpus:
        entry = struct.pack(">II", *cpu) + bytes(24 if wide else 12)
        out += entry
    return out + bytes(64)


def elf(machine, little=True) -> bytes:
    order = "<" if little else ">"
    ident = b"\x7fELF" + bytes([2, 1 if little else 2, 1]) + bytes(9)
    return ident + struct.pack(order + "HH", 1, machine) + bytes(40)


def coff(machine) -> bytes:
    return struct.pack("<H", machine) + bytes(38)


def pe(machine) -> bytes:
    dos = b"MZ" + bytes(0x3A) + struct.pack("<I", 0x40)
    return dos + b"PE\x00\x00" + struct.pack("<H", machine) + bytes(20)


def short_import(machine) -> bytes:
    return struct.pack("<HHHH", 0, 0xFFFF, 0, machine) + bytes(24)


def ar_bsd(*members: tuple[str, bytes]) -> bytes:
    out = b"!<arch>\n"
    for name, data in [("__.SYMDEF SORTED", struct.pack("<I", 0x8664) + bytes(12)), *members]:
        raw = name.encode().ljust((len(name) + 7) // 8 * 8, b"\x00")
        body = raw + data
        header = f"#1/{len(raw):<13}{0:<12}{0:<6}{0:<6}{644:<8}{len(body):<10}".encode() + b"`\n"
        out += header + body + (b"\n" if len(body) % 2 else b"")
    return out


def ar_gnu(*members: tuple[str, bytes]) -> bytes:
    out = b"!<arch>\n"
    symtab = struct.pack(">I", 0xAA64) + bytes(4)  # a count that looks like a machine
    for name, data in [("/", symtab), *[(n + "/", d) for n, d in members]]:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{644:<8}{len(data):<10}".encode() + b"`\n"
        out += header + data + (b"\n" if len(data) % 2 else b"")
    return out


CASES = {
    "thin-arm64": (macho(ARM64), "macho", {"arm64"}),
    "thin-arm64e": (macho(ARM64E), "macho", {"arm64e"}),
    "thin-x86_64-be": (macho(X86_64, little=False), "macho", {"x86_64"}),
    "thin-i386": (macho((7, 3), wide=False), "macho", {"i386"}),
    "fat-universal2": (fat(X86_64, ARM64), "fat", {"x86_64", "arm64"}),
    "fat64-universal2": (fat(X86_64, ARM64, wide=True), "fat", {"x86_64", "arm64"}),
    "elf-x86_64": (elf(62), "elf", {"x86_64"}),
    "elf-aarch64-be": (elf(183, little=False), "elf", {"arm64"}),
    "coff-x86_64": (coff(0x8664), "coff", {"x86_64"}),
    "pe-arm64": (pe(0xAA64), "coff", {"arm64"}),
    "ar-bsd-macho": (ar_bsd(("a.o", macho(ARM64)), ("b.o", macho(ARM64))), "ar", {"arm64"}),
    "ar-bsd-mixed": (
        ar_bsd(("a.o", macho(ARM64)), ("b.o", macho(X86_64))),
        "ar",
        {"arm64", "x86_64"},
    ),
    "ar-gnu-elf": (ar_gnu(("a.o", elf(62)), ("b.o", elf(62))), "ar", {"x86_64"}),
    "ar-coff-import-lib": (ar_gnu(("x.dll", short_import(0x8664))), "ar", {"x86_64"}),
    "bitcode-wrapper": (
        struct.pack("<IIIII", 0x0B17C0DE, 0, 20, 0, 0x0100000C) + bytes(8),
        "bitcode",
        {"arm64"},
    ),
    "tbd": (b"--- !tapi-tbd\ntbd-version: 4\ntargets: [ arm64-macos ]\n", "unknown", set()),
    "java-class": (b"\xca\xfe\xba\xbe\x00\x00\x00\x41" + bytes(40), "unknown", set()),
    "empty": (b"", "unknown", set()),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_reader_formats(tmp_path, name):
    data, fmt, archs = CASES[name]
    path = tmp_path / name
    path.write_bytes(data)
    assert file_archs_of(path) == (fmt, archs)


def test_gnu_ld_script_has_the_architectures_of_the_files_it_names(tmp_path):
    lib = tmp_path / "libm.so.6"
    lib.write_bytes(elf(183))
    script = tmp_path / "libm.so"
    script.write_text(
        f"/* GNU ld script\n*/\nOUTPUT_FORMAT(elf64-littleaarch64)\n"
        f"GROUP ( {lib}  AS_NEEDED ( {tmp_path}/libmvec.so.1 ) )\n"
    )
    assert file_archs_of(script) == ("ldscript", {"arm64"})
    plain = tmp_path / "notes.txt"
    plain.write_text("GROUP is a word here\n")
    assert file_archs_of(plain) == ("unknown", set())


def test_ld_script_in_a_sysroot_names_files_in_the_sysroot(tmp_path):
    root = tmp_path / "sysroot"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "libm.so.6").write_bytes(elf(183))
    script = root / "lib" / "libm.so"
    script.write_text("OUTPUT_FORMAT(elf64-littleaarch64)\nGROUP ( /lib/libm.so.6 )\n")
    assert file_archs_of(script, str(root)) == ("ldscript", {"arm64"})
    assert file_archs_of(script) == ("ldscript", set())


def test_archive_without_members_covers_every_architecture(tmp_path):
    path = tmp_path / "libdl.a"
    path.write_bytes(b"!<arch>\n")
    fmt, archs = file_archs_of(path)
    assert fmt == "ar" and {"arm64", "x86_64"} <= archs


def test_probe_rows_origins_and_missing_file(tmp_path):
    path = tmp_path / "u.a"
    path.write_bytes(fat(X86_64, ARM64))
    out = file_archs(None, "r", (str(path), ""), {})
    assert out["res_file_format"] == [("r", str(path), "fat")]
    assert out["res_file_arch"] == [("r", str(path), "arm64"), ("r", str(path), "x86_64")]
    assert ("res_file_arch", ("r", str(path), "arm64"), str(path)) in out["__origin__"]
    gone = file_archs(None, "r", (str(tmp_path / "gone"), ""), {})
    assert gone["res_file_format"][0][2] == "missing" and gone["res_file_arch"] == []


def test_fingerprint_follows_the_file_stat(tmp_path):
    path = tmp_path / "lib.a"
    path.write_bytes(macho(ARM64))
    before = file_fingerprint((str(path), ""), {})
    assert file_fingerprint((str(path), ""), {}) == before
    path.write_bytes(fat(X86_64, ARM64))  # size changes
    assert file_fingerprint((str(path), ""), {}) != before
    replaced = tmp_path / "new.a"
    replaced.write_bytes(fat(X86_64, ARM64))
    stat = path.stat()
    replaced.replace(path)  # same content and size, another inode
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert file_fingerprint((str(path), ""), {}) != before


def lipo_archs(path) -> set[str]:
    cp = subprocess.run(["lipo", "-info", str(path)], text=True, capture_output=True, check=False)
    text = cp.stdout + cp.stderr
    if "Non-fat file" in text and "architecture:" in text:
        return {text.split("architecture:")[-1].strip()}
    if "are:" in text:
        return set(text.split("are:")[-1].split())
    return set()


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("lipo") or not shutil.which("clang"),
    reason="needs macOS clang and lipo",
)
def test_agrees_with_lipo_on_clang_output(tmp_path):
    src = tmp_path / "a.c"
    src.write_text("int a(void) { return 1; }\n")
    files = []
    for arch in ("arm64", "x86_64"):
        obj = tmp_path / f"a-{arch}.o"
        subprocess.run(["clang", "-arch", arch, "-c", str(src), "-o", str(obj)], check=True)
        lib = tmp_path / f"liba-{arch}.a"
        subprocess.run(["ar", "rcs", str(lib), str(obj)], check=True, capture_output=True)
        dylib = tmp_path / f"liba-{arch}.dylib"
        subprocess.run(
            ["clang", "-arch", arch, "-dynamiclib", str(obj), "-o", str(dylib)], check=True
        )
        files += [obj, lib, dylib]
    for kind in ("a", "dylib"):
        fat_file = tmp_path / f"liba-universal.{kind}"
        subprocess.run(
            [
                "lipo",
                "-create",
                *(str(tmp_path / f"liba-{a}.{kind}") for a in ("arm64", "x86_64")),
                "-output",
                str(fat_file),
            ],
            check=True,
        )
        files.append(fat_file)
    for f in files:
        assert file_archs_of(f)[1] == lipo_archs(f), f
