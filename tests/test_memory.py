"""Memory store tests — offline, no model download, no network.

A deterministic bag-of-words ``FakeEmbedder`` stands in for fastembed so
ranking is exercised end-to-end against a real sqlite-vec store.
"""

from __future__ import annotations

import re

import numpy as np
import pytest

from cophilo.config import get_config
from cophilo.memory import build, search
from cophilo.memory.index import _ensure_fresh
from cophilo.memory.journals import load_journals
from cophilo.memory.store import connect, current_signature, get_by_name

pytest.importorskip("sqlite_vec")

JOURNALS_YAML = """\
journals:
  - name: "Journal of Free Will"
    publisher: "Acme"
    issn: null
    scope: "Determinism, moral responsibility, and the ability to do otherwise."
    typical_length: "~8000 words"
    max_words: 9000
    open_access: true
    url: "https://example.org/jfw"
    sources: ["knowledge"]
  - name: "Phenomenology Review"
    publisher: "Brill"
    issn: null
    scope: "Consciousness, perception, and the phenomenology of mind."
    typical_length: "~9000 words"
    max_words: null
    open_access: false
    url: "https://example.org/phenom"
    sources: ["knowledge"]
  - name: "Symbolic Logic Quarterly"
    publisher: "Springer"
    issn: null
    scope: "Formal logic, proof theory, and computation."
    typical_length: "varies"
    max_words: null
    open_access: false
    url: "https://example.org/slq"
    sources: ["knowledge"]
"""

_TOKEN = re.compile(r"[a-z]+")


class FakeEmbedder:
    """Hashing bag-of-words embedder: shared vocabulary → high cosine."""

    name = "fake-test-embedder"
    dim = 64

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in _TOKEN.findall(t.lower()):
                out[i, hash(tok) % self.dim] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "journals.yaml").write_text(JOURNALS_YAML, encoding="utf-8")
    monkeypatch.setenv("COPHILO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COPHILO_MEMORY_DB_PATH", str(data_dir / "db" / "memory.sqlite"))
    get_config.cache_clear()
    yield get_config()
    get_config.cache_clear()


def test_load_journals_parses_and_dedupes(isolated_data_dir):
    recs = load_journals(isolated_data_dir)
    assert [r.name for r in recs] == [
        "Journal of Free Will",
        "Phenomenology Review",
        "Symbolic Logic Quarterly",
    ]
    jfw = recs[0]
    assert jfw.slug == "journal-of-free-will"
    assert jfw.open_access is True
    assert jfw.max_words == 9000
    assert recs[2].open_access is False


def test_build_then_search_ranks_topically(isolated_data_dir):
    emb = FakeEmbedder()
    assert build(isolated_data_dir, embedder=emb) == 3

    hits = search(
        isolated_data_dir,
        "determinism and moral responsibility",
        limit=3,
        embedder=emb,
    )
    assert hits[0]["name"] == "Journal of Free Will"
    assert hits[0]["score"] >= hits[-1]["score"]


def test_open_access_filter(isolated_data_dir):
    emb = FakeEmbedder()
    hits = search(
        isolated_data_dir,
        "consciousness and perception phenomenology",
        limit=5,
        open_access_only=True,
        embedder=emb,
    )
    assert hits, "expected at least the open-access journal"
    assert all(h["open_access"] for h in hits)


def test_empty_query_rejected(isolated_data_dir):
    with pytest.raises(ValueError):
        search(isolated_data_dir, "   ", embedder=FakeEmbedder())


def test_ensure_fresh_rebuilds_on_catalog_change(isolated_data_dir):
    emb = FakeEmbedder()
    build(isolated_data_dir, embedder=emb)
    conn = connect(isolated_data_dir.memory_db_path)
    sig1 = current_signature(conn)
    conn.close()

    # Unchanged catalog → signature stable, no rebuild.
    _ensure_fresh(isolated_data_dir, emb)
    conn = connect(isolated_data_dir.memory_db_path)
    assert current_signature(conn) == sig1
    conn.close()

    # Edit the catalog → signature changes and search sees the new journal.
    path = isolated_data_dir.data_dir / "journals.yaml"
    path.write_text(
        JOURNALS_YAML
        + '  - name: "Ethics of Technology"\n'
        '    scope: "Moral questions raised by emerging technology."\n'
        "    open_access: true\n"
        '    url: "https://example.org/eot"\n',
        encoding="utf-8",
    )
    hits = search(
        isolated_data_dir, "ethics of emerging technology", limit=4, embedder=emb
    )
    assert any(h["name"] == "Ethics of Technology" for h in hits)
    conn = connect(isolated_data_dir.memory_db_path)
    assert current_signature(conn) != sig1
    conn.close()


def test_get_by_name_exact_and_fuzzy(isolated_data_dir):
    build(isolated_data_dir, embedder=FakeEmbedder())
    conn = connect(isolated_data_dir.memory_db_path)
    try:
        assert get_by_name(conn, "Journal of Free Will")["open_access"] is True
        assert get_by_name(conn, "phenomenology")["name"] == "Phenomenology Review"
        assert get_by_name(conn, "no such journal") is None
    finally:
        conn.close()
