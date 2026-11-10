# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`buildlog -E`: portable, language-neutral file commands for build actions.

In the spirit of `cmake -E`: one spelling on every host, no shell. Actions
name them with L0's action_builtin(a, name); the names must match L0's
builtin_command facts (tests check both lists).

  collate PACKAGE:STRATUM ARGS...
                               run a plugin's build-time stratum (stratum.py):
                               reads the build's scan results and writes the
                               dyndep, module maps and provenance of C++
                               modules (buildlog-cpp's collator)
  copy SRC DST                 copy a file (contents and mode)
  copy_if_different SRC DST    copy only if DST is missing or differs, so a
                               restat action leaves an unchanged DST alone
  create_symlink TARGET LINK   (re)create LINK pointing at TARGET
  make_directory DIR...        create directories and their parents
  remove PATH...               remove files; missing ones are not an error
  touch FILE...                create files or update their times
  configure_file -o OUT [--at-only] [-DNAME=VALUE | -UNAME]... PIECE...
                               substitute variables into a template, CMake's
                               configure_file dialect (L1_configure_file.dl);
                               OUT is written only if it changes
  export_package --format FMT -o OUT ...
                               write one export file of an installed package
                               (L3_install.dl; buildlog.export_formats)
  install (--prefix P | --no-prefix) [--file SRC DEST]...
                               copy each SRC to $DESTDIR/DEST (DEST is
                               absolute: the prefix and the layout,
                               L3_install.dl); files that are already
                               identical are left alone. Several SRCs of one
                               DEST must have identical contents (checked
                               before copying anything). A SRC that is a
                               symlink to a file beside it (a shared
                               library's links) installs as the same link.
                               --no-prefix: the
                               user stated no install prefix, which is an
                               error (there is no default)
"""

import filecmp
import hashlib
import os
import re
import shutil
import sys

from .export_formats import ExportError, export_package
from .stratum import StratumError, collate


def _copy(src, dst):
    shutil.copyfile(src, dst)
    shutil.copymode(src, dst)


def copy(src, dst):
    _copy(src, dst)


def copy_if_different(src, dst):
    if not (os.path.isfile(dst) and filecmp.cmp(src, dst, shallow=False)):
        _copy(src, dst)


def create_symlink(target, link):
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(target, link)


def make_directory(*dirs):
    for d in dirs:
        os.makedirs(d, exist_ok=True)


def remove(*paths):
    for p in paths:
        if os.path.lexists(p):
            os.remove(p)


def touch(*files):
    for f in files:
        with open(f, "a"):
            os.utime(f)


# CMake's configure_file dialect, as far as zlib uses it (L1_configure_file.dl).
AT_VAR = re.compile(r"@([A-Za-z_0-9/.+-]+)@")
BRACE_VAR = re.compile(r"\$\{([A-Za-z_0-9/.+-]+)\}")
CMAKEDEFINE = re.compile(r"#([ \t]*)cmakedefine([ \t]+)([A-Za-z_0-9]*)")
CMAKEDEFINE01 = re.compile(r"#[ \t]*cmakedefine01[ \t]")
# Values CMake's #cmakedefine reads as false (cmIsOff); such a variable is
# stated undefined (-U), not defined to a false value.
CMAKE_FALSE = re.compile(r"(0|OFF|NO|FALSE|N|IGNORE|NOTFOUND|.*-NOTFOUND)?", re.IGNORECASE)


class ConfigureError(Exception):
    pass


def _configure_line(line, defined, undefined, at_only, where):
    def value(m):
        name = m.group(1)
        if name in defined:
            return defined[name]
        state = "undefined (-U)" if name in undefined else "not given"
        raise ConfigureError(f"{where}: variable {name} is {state}")

    if CMAKEDEFINE01.search(line):
        raise ConfigureError(f"{where}: #cmakedefine01 is not supported")
    m = CMAKEDEFINE.search(line)
    if m:
        ws, sep, name = m.groups()
        if name in defined:
            if CMAKE_FALSE.fullmatch(defined[name]):
                raise ConfigureError(
                    f"{where}: #cmakedefine {name}: {name}={defined[name]} is false to CMake; "
                    f"state -U{name}"
                )
            line = line[: m.start()] + f"#{ws}define{sep}{name}" + line[m.end() :]
        elif name in undefined:
            end = "\n" if line.endswith("\n") else ""
            line = f"/* #{ws}undef {name} */{end}"
        else:
            raise ConfigureError(
                f"{where}: #cmakedefine {name}: {name} is neither defined nor undefined"
            )
    line = AT_VAR.sub(value, line)
    if not at_only:
        line = BRACE_VAR.sub(value, line)
    return line


def configure_file(*argv):
    """configure_file -o OUT [--at-only] [-DNAME=VALUE | -UNAME]... PIECE...
    PIECE: --lines=FIRST:LAST:PATH (lines FIRST..LAST of PATH, 1-based; LAST 0:
    to the end) or --text=LINE (one line). OUT is written only if it changes."""
    out, at_only, defined, undefined, template = None, False, {}, set(), []
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "-o":
            out = args.pop(0)
        elif a == "--at-only":
            at_only = True
        elif a.startswith("-D"):
            name, eq, val = a[2:].partition("=")
            if not eq or name in defined and defined[name] != val:
                raise ConfigureError(f"bad or conflicting definition {a}")
            defined[name] = val
        elif a.startswith("-U"):
            undefined.add(a[2:])
        elif a.startswith("--lines="):
            first, last, path = a[len("--lines=") :].split(":", 2)
            with open(path, newline="") as f:
                lines = f.read().splitlines(keepends=True)
            first, last = int(first), int(last) or len(lines)
            if not 1 <= first <= last <= len(lines):
                raise ConfigureError(f"{path}: lines {first}..{last} outside 1..{len(lines)}")
            template += [
                (f"{path}:{first + k}", ln) for k, ln in enumerate(lines[first - 1 : last])
            ]
        elif a.startswith("--text="):
            text = a[len("--text=") :].replace("\u2028", "\n")
            template += [("text", ln + "\n") for ln in text.split("\n")]
        else:
            raise ConfigureError(f"unknown argument {a}")
    both = sorted(undefined & set(defined))
    if out is None or both:
        raise ConfigureError(f"no -o, or defined and undefined: {', '.join(both)}")
    text = "".join(_configure_line(ln, defined, undefined, at_only, w) for w, ln in template)
    if os.path.isfile(out):
        with open(out, newline="") as f:
            if f.read() == text:
                return
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="") as f:
        f.write(text)


NO_PREFIX = (
    "no install prefix. Where to install is the installer's decision and has no "
    "default: state it in a user fact file, e.g. prefix.toml with the line\n"
    '    install_prefix = "/opt/example"\n'
    "then reconfigure with `buildlog configure --user prefix.toml ...` and run the "
    "install target again. DESTDIR=... stages the install under another root."
)


class InstallError(Exception):
    pass


def install(*argv):
    """install (--prefix P | --no-prefix) [--file SRC DEST]...: copy each SRC
    to DEST under $DESTDIR (a packager's staging root)."""
    args = list(argv)
    if not args or args[0] not in ("--prefix", "--no-prefix"):
        raise InstallError("first argument must be --prefix P or --no-prefix")
    if args.pop(0) == "--no-prefix":
        raise InstallError(NO_PREFIX)
    prefix = args.pop(0)
    destdir = os.environ.get("DESTDIR", "")
    pairs = []
    while args:
        if args[0] != "--file" or len(args) < 3:
            raise InstallError(f"expected --file SRC DEST, got {' '.join(args[:3])}")
        pairs.append((args[1], args[2]))
        del args[:3]
    by_dest: dict[str, list[str]] = {}
    for src, dest in pairs:
        if not os.path.isabs(dest) or not dest.startswith(prefix.rstrip("/") + "/"):
            raise InstallError(f"{dest} is not under the prefix {prefix}")
        by_dest.setdefault(dest, []).append(src)
    # Several configurations may install to one destination only identical
    # content (L3_install.dl, "multi-configuration installs"): checked for
    # every destination before anything is copied.
    for dest, srcs in by_dest.items():
        digests = {src: _digest(src) for src in srcs}
        if len(set(digests.values())) > 1:
            listed = ", ".join(f"{s} ({d[:12]})" for s, d in sorted(digests.items()))
            raise InstallError(f"different contents for {dest}: {listed}")
    for dest, srcs in by_dest.items():
        target = os.path.join(destdir, dest.lstrip("/")) if destdir else dest
        os.makedirs(os.path.dirname(target), exist_ok=True)
        link = _sibling_link(srcs[0])
        if link is not None:
            if not (os.path.islink(target) and os.readlink(target) == link):
                create_symlink(link, target)
        else:
            copy_if_different(srcs[0], target)
    where = f"{destdir}{prefix}" if destdir else prefix
    print(f"installed {len(by_dest)} files under {where}")


def _sibling_link(path):
    """The target of a symlink to a file in its own directory, else None."""
    if not os.path.islink(path):
        return None
    to = os.readlink(path)
    return to if "/" not in to else None


def _digest(path):
    link = _sibling_link(path)
    if link is not None:
        return hashlib.sha256(f"symlink:{link}".encode()).hexdigest()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


COMMANDS = {
    "collate": (collate, 1, None),
    "copy": (copy, 2, 2),
    "copy_if_different": (copy_if_different, 2, 2),
    "configure_file": (configure_file, 1, None),
    "create_symlink": (create_symlink, 2, 2),
    "export_package": (export_package, 1, None),
    "install": (install, 1, None),
    "make_directory": (make_directory, 1, None),
    "remove": (remove, 1, None),
    "touch": (touch, 1, None),
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        names = ", ".join(sorted(COMMANDS))
        print(f"usage: buildlog -E <command> [args...]; commands: {names}", file=sys.stderr)
        return 2
    fn, lo, hi = COMMANDS[argv[0]]
    args = argv[1:]
    if len(args) < lo or (hi is not None and len(args) > hi):
        print(f"buildlog -E {argv[0]}: wrong number of arguments", file=sys.stderr)
        return 2
    try:
        fn(*args)
    except (OSError, ConfigureError, InstallError, ExportError, StratumError) as exc:
        print(f"buildlog -E {argv[0]}: {exc}", file=sys.stderr)
        return 1
    return 0
