"""Tests for the cross-document concept graph (REPORT.md §2.3):
the DB helpers and the `cophilo graph` CLI."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cophilo.cli import app
from cophilo.config import ensure_dirs, get_config
from cophilo.db import models as db


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("COPHILO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COPHILO_DB_PATH", str(data_dir / "db" / "cophilo.sqlite"))
    get_config.cache_clear()
    cfg = get_config()
    ensure_dirs(cfg)
    db.init_db(cfg)
    yield cfg
    get_config.cache_clear()


# --- fixtures ---------------------------------------------------------------


def _add_doc(conn, *, title: str, source_path: str, kind: str = "note") -> int:
    return db.insert_document(
        conn,
        kind=kind,
        title=title,
        source_path=source_path,
        normalized_path=None,
        language="en",
        metadata=None,
    )


def _add_passage(conn, *, document_id: int, ord_: int, text: str = "x") -> int:
    cur = conn.execute(
        """INSERT INTO passages (document_id, ord, char_start, char_end, text)
           VALUES (?, ?, ?, ?, ?);""",
        (document_id, ord_, 0, len(text), text),
    )
    return int(cur.lastrowid)


def _add_concept(conn, *, slug: str, label_en: str) -> int:
    cur = conn.execute(
        """INSERT INTO concepts (slug, canonical_label_en, kind, status, created_at,
                                 confirmed_at)
           VALUES (?, ?, 'mine', 'confirmed', '2026-01-01T00:00:00+00:00',
                   '2026-01-01T00:00:00+00:00');""",
        (slug, label_en),
    )
    return int(cur.lastrowid)


def _mention_concept(conn, *, concept_id: int, passage_id: int) -> None:
    conn.execute(
        "INSERT INTO concept_mentions (concept_id, passage_id) VALUES (?, ?);",
        (concept_id, passage_id),
    )


def _add_question(conn, *, label: str, status: str = "open") -> int:
    cur = conn.execute(
        "INSERT INTO questions (label, status) VALUES (?, ?);",
        (label, status),
    )
    return int(cur.lastrowid)


def _mention_question(conn, *, question_id: int, passage_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO question_mentions (question_id, passage_id) "
        "VALUES (?, ?);",
        (question_id, passage_id),
    )


@pytest.fixture
def seeded_corpus(isolated_data_dir):
    """Three documents, four concepts, two questions — enough to exercise
    document-level co-occurrence and the drill-down view."""
    cfg = isolated_data_dir
    with db.transaction(cfg) as conn:
        d1 = _add_doc(conn, title="Note on forgetting", source_path="/n/1.md")
        d2 = _add_doc(conn, title="Funes redux", source_path="/n/2.md")
        d3 = _add_doc(conn, title="Off-topic political note", source_path="/n/3.md")

        p1a = _add_passage(conn, document_id=d1, ord_=0)
        p1b = _add_passage(conn, document_id=d1, ord_=1)
        p2 = _add_passage(conn, document_id=d2, ord_=0)
        p3 = _add_passage(conn, document_id=d3, ord_=0)

        c_forget = _add_concept(conn, slug="forgetting", label_en="Forgetting")
        c_funes = _add_concept(conn, slug="funes", label_en="Funes")
        c_memory = _add_concept(conn, slug="memory", label_en="Memory")
        c_state = _add_concept(conn, slug="state", label_en="State")

        # Forgetting + memory co-occur in d1 (twice, but co-occurrence is
        # at document level so still counts as one shared doc):
        _mention_concept(conn, concept_id=c_forget, passage_id=p1a)
        _mention_concept(conn, concept_id=c_forget, passage_id=p1b)
        _mention_concept(conn, concept_id=c_memory, passage_id=p1a)
        # Forgetting + funes + memory all in d2:
        _mention_concept(conn, concept_id=c_forget, passage_id=p2)
        _mention_concept(conn, concept_id=c_funes, passage_id=p2)
        _mention_concept(conn, concept_id=c_memory, passage_id=p2)
        # State only appears in d3 — never co-occurs with the others:
        _mention_concept(conn, concept_id=c_state, passage_id=p3)

        # Two questions: one shared by d1+d2 (where forgetting/memory live),
        # one only in d3.
        q_cross = _add_question(conn, label="Is forgetting principled or lossy?")
        q_local = _add_question(conn, label="What is the state's role?")
        _mention_question(conn, question_id=q_cross, passage_id=p1a)
        _mention_question(conn, question_id=q_cross, passage_id=p2)
        _mention_question(conn, question_id=q_local, passage_id=p3)
    return cfg


# --- DB helpers -------------------------------------------------------------


def test_list_concepts_with_spread_ranks_by_doc_count(seeded_corpus):
    cfg = seeded_corpus
    with db.transaction(cfg) as conn:
        rows = db.list_concepts_with_spread(conn)
    by_slug = {r["slug"]: r for r in rows}
    # Forgetting hits 2 docs (d1, d2) with 3 mentions; memory: 2 docs, 2 mentions;
    # funes & state: 1 doc each.
    assert by_slug["forgetting"]["doc_count"] == 2 and by_slug["forgetting"]["mentions"] == 3
    assert by_slug["memory"]["doc_count"] == 2 and by_slug["memory"]["mentions"] == 2
    assert by_slug["funes"]["doc_count"] == 1
    assert by_slug["state"]["doc_count"] == 1
    # Ordering: doc_count DESC, then mentions DESC. Forgetting must come first.
    assert rows[0]["slug"] == "forgetting"
    assert rows[1]["slug"] == "memory"


def test_concept_co_occurrences_at_document_level(seeded_corpus):
    """Two concepts in the same passage count once; in the same document
    across different passages also count once."""
    cfg = seeded_corpus
    with db.transaction(cfg) as conn:
        pairs = db.concept_co_occurrences(conn, min_shared=2, limit=20)
    # Only forgetting↔memory share ≥2 documents (d1, d2). Funes co-occurs in
    # one doc only.
    assert len(pairs) == 1
    p = pairs[0]
    slugs = {p["a_slug"], p["b_slug"]}
    assert slugs == {"forgetting", "memory"}
    assert p["shared"] == 2

    # Lower the threshold and we surface the funes pairs too.
    with db.transaction(cfg) as conn:
        loose = db.concept_co_occurrences(conn, min_shared=1, limit=20)
    pair_slugs = {tuple(sorted([p["a_slug"], p["b_slug"]])) for p in loose}
    assert ("forgetting", "memory") in pair_slugs
    assert ("forgetting", "funes") in pair_slugs
    assert ("funes", "memory") in pair_slugs
    # State is in its own doc — pairs with nothing.
    assert not any("state" in pair for pair in pair_slugs)


def test_find_concept_exact_slug_label_and_substring(seeded_corpus):
    cfg = seeded_corpus
    with db.transaction(cfg) as conn:
        # Exact slug:
        assert db.find_concept(conn, "forgetting")["slug"] == "forgetting"
        # Exact English label (case-insensitive):
        assert db.find_concept(conn, "MEMORY")["slug"] == "memory"
        # Substring on slug:
        assert db.find_concept(conn, "forget")["slug"] == "forgetting"
        # Miss:
        assert db.find_concept(conn, "extended-mind") is None
        # Whitespace / empty:
        assert db.find_concept(conn, "   ") is None


def test_concept_neighbors_and_questions(seeded_corpus):
    cfg = seeded_corpus
    with db.transaction(cfg) as conn:
        forget = db.find_concept(conn, "forgetting")
        neighbors = db.concept_neighbors(conn, forget["id"], limit=10)
        qs = db.concept_questions(conn, forget["id"], limit=10)
        docs = db.concept_docs(conn, forget["id"])

    # Forgetting lives in d1+d2: memory shares both, funes only d2.
    by_slug = {n["slug"]: n for n in neighbors}
    assert by_slug["memory"]["shared_docs"] == 2
    assert by_slug["funes"]["shared_docs"] == 1
    # State isn't a neighbor (no shared doc):
    assert "state" not in by_slug

    # The cross-doc question reaches both d1 and d2; the d3-only one doesn't:
    labels = [q["label"] for q in qs]
    assert "Is forgetting principled or lossy?" in labels
    assert "What is the state's role?" not in labels

    # Documents listing: both d1 and d2, ranked by mention count (d1 has 2).
    assert [d["title"] for d in docs] == ["Note on forgetting", "Funes redux"]
    assert docs[0]["mentions"] == 2 and docs[1]["mentions"] == 1


# --- CLI --------------------------------------------------------------------


runner = CliRunner()


def test_cli_graph_overview_human_and_json(seeded_corpus):
    res = runner.invoke(app, ["graph"])
    assert res.exit_code == 0, res.output
    assert "Top concepts by document spread" in res.output
    # Forgetting and memory are top of spread (2 docs each):
    out = res.output
    assert "Forgetting" in out and "Memory" in out
    # Co-occurrence section shows the forgetting↔memory pair (2 docs):
    assert "Top co-occurring concept pairs" in out
    assert "↔" in out

    j = runner.invoke(app, ["graph", "--json"])
    assert j.exit_code == 0
    payload = json.loads(j.output)
    assert "concepts" in payload and "co_occurrences" in payload
    by_slug = {c["slug"]: c for c in payload["concepts"]}
    assert by_slug["forgetting"]["doc_count"] == 2
    pair_slugs = {tuple(sorted([p["a_slug"], p["b_slug"]])) for p in payload["co_occurrences"]}
    assert ("forgetting", "memory") in pair_slugs


def test_cli_graph_drill_into_concept(seeded_corpus):
    res = runner.invoke(app, ["graph", "forgetting"])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "Concept: Forgetting" in out
    assert "Appears in 2 document(s)" in out
    assert "Note on forgetting" in out and "Funes redux" in out
    # Neighbors:
    assert "Co-occurring concepts" in out
    assert "Memory" in out and "Funes" in out
    assert "State" not in out  # never shares a doc with forgetting
    # Questions: cross-doc question surfaces; d3-only one doesn't.
    assert "Is forgetting principled or lossy?" in out
    assert "What is the state's role?" not in out


def test_cli_graph_unknown_concept_exits_nonzero(seeded_corpus):
    res = runner.invoke(app, ["graph", "extended-mind"])
    assert res.exit_code != 0
    assert "No confirmed concept" in (res.output + (res.stderr or ""))


def test_cli_graph_empty_corpus_message(isolated_data_dir):
    """With nothing extracted yet, the overview tells the user where to start
    instead of printing blank sections."""
    res = runner.invoke(app, ["graph"])
    assert res.exit_code == 0, res.output
    assert "No confirmed concepts yet" in res.output
    assert "cophilo extract" in res.output
