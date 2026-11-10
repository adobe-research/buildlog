# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The round memo key is a function of the fact directory's names and bytes
(source_map.json aside), however the digests were obtained."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from buildlog import engine
from buildlog.engine import SouffleRounds
from buildlog.paths import souffle_path

from .test_souffle_rounds import _configure

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


def _dir(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    for name, text in files.items():
        (path / name).write_text(text)
    return path


def _key(facts: Path) -> str:
    return SouffleRounds(facts / "p.dl", "digest")._inputs_key(facts)


def test_key_follows_names_and_bytes(tmp_path):
    base = {"a.facts": "x\n", "b.facts": "y\n"}
    k = _key(_dir(tmp_path / "1", base))
    assert _key(_dir(tmp_path / "2", base)) == k
    assert _key(_dir(tmp_path / "3", {**base, "source_map.json": "{}"})) == k
    assert _key(_dir(tmp_path / "4", {**base, "b.facts": "z\n"})) != k
    assert _key(_dir(tmp_path / "5", {**base, "c.facts": ""})) != k
    assert _key(_dir(tmp_path / "6", {"a.facts": "x\n"})) != k
    # Content moved between names, or across the name boundary.
    assert _key(_dir(tmp_path / "7", {"a.facts": "y\n", "b.facts": "x\n"})) != k
    assert _key(_dir(tmp_path / "8", {"a.facts": "x\ny\n"})) != k


def test_writer_digests_never_override_the_directory(tmp_path):
    facts = _dir(tmp_path / "f", {"a.facts": "x\n", "b.facts": "y\n"})
    honest = _key(facts)
    engine._WRITTEN_DIGESTS[str(facts)] = {
        n: (hashlib.sha256((facts / n).read_bytes()).hexdigest(), (facts / n).stat().st_size)
        for n in ("a.facts", "b.facts")
    }
    assert _key(facts) == honest
    # A changed size, an added file: hashed again, not trusted.
    (facts / "a.facts").write_text("xx\n")
    (facts / "c.facts").write_text("z\n")
    stale = _key(facts)
    engine._WRITTEN_DIGESTS.pop(str(facts))
    assert stale == _key(facts)


@pytest.mark.integration
def test_materialized_digests_match_the_files(tmp_path, monkeypatch):
    build = tmp_path / "build"
    _configure(build, monkeypatch)
    for facts in (build / "facts-in").iterdir():
        known = engine._WRITTEN_DIGESTS[str(facts)]
        names = {f.name for f in facts.iterdir()} - {"source_map.json"}
        assert known.keys() == names
        for name, (digest, size) in known.items():
            data = (facts / name).read_bytes()
            assert (hashlib.sha256(data).hexdigest(), len(data)) == (digest, size)
