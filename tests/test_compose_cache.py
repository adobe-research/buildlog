# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The composition memo (compose_cache): a hit gives the program a miss would,
and any change to what composition reads is a miss."""

import pickle

import pytest

from buildlog import compose_cache, layers
from buildlog.layers import compose_program
from buildlog.paths import souffle_path

from .test_layers import write_layer

pytestmark = pytest.mark.skipif(not souffle_path().exists(), reason="patched Souffle not installed")


def tree(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class Setup:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.lower = write_layer(
            tmp_path, "lower", 0, '.decl r(x:symbol)\nr("a").\n', exports=["r"]
        )
        self.upper = write_layer(
            tmp_path,
            "upper",
            1,
            ".decl s(x:symbol)\ns(x) :- r(x).\n",
            requires={"lower": ">=0.1"},
        )
        self.project = tmp_path / "proj.dl"
        self.project.write_text('.decl pt(x:symbol)\npt("a").\n')
        self.cache = tmp_path / "cache"

    def compose(self, name="p", project=True, **kw):
        return compose_program(
            [self.lower, self.upper],
            self.root / name,
            self.root / f"{name}-work",
            project_files=[self.project] if project else None,
            cache_dir=self.cache,
            **kw,
        )


@pytest.fixture
def s(tmp_path):
    return Setup(tmp_path)


def test_hit_gives_the_misses_program(s):
    miss = s.compose()
    assert miss.errors == [] and not miss.cached
    before = tree(s.root / "p")
    hit = s.compose()
    assert hit.cached
    assert tree(s.root / "p") == before
    assert pickle.dumps(hit.layers) == pickle.dumps(miss.layers)
    assert hit.build == miss.build
    assert (hit.inputs, hit.outputs, hit.driver_inputs) == (
        miss.inputs,
        miss.outputs,
        miss.driver_inputs,
    )
    assert (s.root / "p-work").is_dir()


def test_hit_does_not_compose(s, monkeypatch):
    s.compose()

    def boom(*a, **k):
        raise AssertionError("composed on a hit")

    monkeypatch.setattr(layers, "_compose_uncached", boom)
    assert s.compose().cached


def test_hit_restores_removed_files(s):
    s.compose()
    (s.root / "p" / "io.dl").unlink()
    (s.root / "p" / "stale.dl").write_text("junk")
    s.compose()
    assert (s.root / "p" / "io.dl").is_file() and not (s.root / "p" / "stale.dl").exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda s: (s.lower.parent / "layer.dl").write_text('.decl r(x:symbol)\nr("b").\n'),
        lambda s: (s.upper.parent / "layer.dl").write_text(
            '.decl s(x:symbol)\ns(x) :- r(x), x != "z".\n'
        ),
        lambda s: s.upper.write_text(s.upper.read_text() + "# touched\n"),
        lambda s: s.project.write_text('.decl pt(x:symbol)\npt("b").\n'),
        lambda s: s.lower.write_text(s.lower.read_text().replace('"0.1.0"', '"0.2.0"')),
    ],
    ids=["lower-layer", "upper-layer", "manifest", "project-file", "version"],
)
def test_changed_input_misses(s, change):
    assert not s.compose().cached
    assert s.compose().cached
    change(s)
    assert not s.compose().cached


def test_content_not_mtime(s):
    layer = s.lower.parent / "layer.dl"
    s.compose()
    text = layer.read_text()
    layer.write_text(text)  # new mtime, same content
    assert s.compose().cached
    layer.write_text(text.replace('"a"', '"b"'))
    st = layer.stat()
    layer.write_text(text.replace('"a"', '"c"'))
    import os

    os.utime(layer, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size, same mtime
    assert not s.compose().cached


def test_other_arguments_miss(s, tmp_path):
    s.compose()
    assert not s.compose(project=False).cached
    assert not s.compose(name="elsewhere").cached
    assert not s.compose(required_outputs=("s",)).cached


def test_explicit_schema_scope_is_in_cache_key(s):
    assert not s.compose().cached
    scoped = s.compose(explicit_manifests=frozenset({s.lower}))
    assert not scoped.cached and not scoped.errors
    assert {layer.name for layer in scoped.schema_layers} == {"lower", "project"}
    assert s.compose(explicit_manifests=frozenset({s.lower})).cached
    assert s.compose().cached


def test_source_digest_is_in_the_key(s, monkeypatch):
    s.compose()
    monkeypatch.setattr(compose_cache, "_SOURCE", "another loader")
    assert not s.compose().cached


def test_errors_are_not_cached(s):
    (s.upper.parent / "layer.dl").write_text("s(x) :- undefined_rel(x).\n")
    first = s.compose()
    assert first.errors and first.build is None
    second = s.compose()
    assert not second.cached and second.errors == first.errors
    assert not s.cache.exists() or not list(s.cache.glob("compose-*.pkl"))


def test_corrupt_entry_is_a_miss(s):
    s.compose()
    (entry,) = s.cache.glob("compose-*.pkl")
    entry.write_bytes(b"not a pickle")
    assert not s.compose().cached
    assert s.compose().cached


def test_disabled_by_environment(s, monkeypatch):
    monkeypatch.setenv(compose_cache.ENV, "0")
    s.compose()
    assert not s.compose().cached
    assert not s.cache.exists()
