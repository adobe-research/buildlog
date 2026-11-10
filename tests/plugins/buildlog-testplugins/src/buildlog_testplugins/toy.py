# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A toy package provider: ids `toy:<name>`, files `<name>.toy` under
`<prefix>/share/toy` or a search directory. One statement per line:
`interface <id>`, `dep <from> <to>`, `executable <id> <path>` (an imported
executable at path, relative to the file), `version <id> <major> <minor>
<patch>`."""

from __future__ import annotations

from pathlib import Path

from buildlog.providers import TRUE, MetadataError, MetadataLinks, stat_token


class Toy:
    def _dirs(self, kind: str, root: Path) -> list[Path]:
        return [root / "share" / "toy"] if kind == "prefix" else [root]

    def find(self, name: str, kind: str, root: Path) -> list[Path]:
        if not name.startswith("toy:"):
            return []
        files = [d / f"{name[4:]}.toy" for d in self._dirs(kind, root)]
        return [f for f in files if f.is_file()]

    def find_watched(self, name: str, kind: str, root: Path) -> list:
        return [stat_token(d / f"{name[4:]}.toy") for d in self._dirs(kind, root)]

    def enumerate_files(self, kind, root, observations):
        return [
            file
            for directory in self._dirs(kind, root)
            for file in observations.directory(directory)
            if file.suffix == ".toy" and file.is_file()
        ]

    def metadata_links(self, file, observations):
        observations.file(file)
        return MetadataLinks()

    def offers(self, file, kind, root, names, observations):
        if file.parent in self._dirs(kind, root) and file.suffix == ".toy":
            return {f"toy:{file.stem}"}
        return set()

    def decode(self, res, vocabulary, namespace):
        self.load(res, None, namespace)

    def load(self, res, results, namespace: str) -> None:
        if namespace:
            raise MetadataError(None, "toy files have no namespaces")
        for n, line in enumerate(res.file.read_text().splitlines(), 1):
            words = line.split()
            if words[:1] == ["interface"] and len(words) == 2:
                res.target(words[1], "interface", n)
            elif words[:1] == ["dep"] and len(words) == 3:
                res.dep(words[1], words[2], "public", TRUE, n)
            elif words[:1] == ["executable"] and len(words) == 3:
                res.target(words[1], "executable", n)
                res.location(words[1], str((res.file.parent / words[2]).resolve()), TRUE, n)
            elif words[:1] == ["version"] and len(words) == 5:
                res.version(words[1], *words[2:], n)
            elif words:
                raise MetadataError(n, f"unknown toy line {line!r}")

    def watched(self, path: Path, results) -> list:
        return [stat_token(path)]


TOY = Toy()
