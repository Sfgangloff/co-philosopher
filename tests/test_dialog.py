"""Tests for the offline `cophilo dialog` capture REPL and its hand-off to
the Markdown ingester. No network, no API, no TTY."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import frontmatter
import pytest

from cophilo.config import ensure_dirs, get_config
from cophilo.db import models as db
from cophilo.ingest.dispatch import ingest_tree
from cophilo.notes.capture import DialogSession, run_dialog


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


_CLOCK = lambda: datetime(2026, 5, 17, 14, 32, 0)  # noqa: E731


def test_session_creates_then_appends(isolated_data_dir):
    cfg = isolated_data_dir
    s = DialogSession(cfg=cfg, topic="free will", clock=_CLOCK)

    status1, p1 = s.add("Determinism doesn't threaten moral responsibility.")
    status2, p2 = s.add("But Frankfurt cases complicate the picture.")

    assert status1 == "saved" and status2 == "appended"
    assert p1 == p2
    assert p1.name == "2026-05-17-1432-free-will.md"
    assert p1.parent == cfg.corpus_notes_dir

    post = frontmatter.loads(p1.read_text(encoding="utf-8"))
    assert post["kind"] == "note"
    assert post["title"] == "free will"
    assert post["topic"] == "free will"
    assert post["language"] in {"en", "fr"}
    assert "Determinism" in post.content
    assert "Frankfurt" in post.content
    assert s.total_entries == 2 and len(s.files) == 1


def test_new_and_cancel(isolated_data_dir):
    s = DialogSession(cfg=isolated_data_dir, clock=_CLOCK)
    _, first = s.add("first note")
    s.new_note()
    _, second = s.add("second note")
    assert first != second
    assert len(s.files) == 2

    discarded = s.cancel()
    assert discarded == second
    assert not second.exists()
    assert first.exists()


def test_run_dialog_scripted(isolated_data_dir):
    cfg = isolated_data_dir
    script = iter(
        [
            "note about compatibilism",
            "",  # blank lines are ignored
            "/new",
            "a separate thought",
            "/cancel",
            "/done",
        ]
    )
    out: list[str] = []
    session = run_dialog(
        cfg,
        topic="liberty",
        input_fn=lambda _prompt: next(script),
        echo=out.append,
        clock=_CLOCK,
    )
    # one file survived (the /cancel discarded the second)
    assert len(session.files) == 1
    assert session.files[0].exists()
    assert "Saved" in session.summary()
    assert any("discarded" in line for line in out)


def test_capture_then_ingest_roundtrip(isolated_data_dir):
    cfg = isolated_data_dir
    s = DialogSession(cfg=cfg, topic="self-knowledge", clock=_CLOCK)
    s.add("Self-knowledge is not transparent introspection.")

    outcomes = ingest_tree(cfg, cfg.corpus_dir)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.status == "new" and o.kind == "note"

    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?;", (o.doc_id,)
        ).fetchone()
    assert row["kind"] == "note"
    assert row["status"] == "ingested"

    # re-ingest is a no-op now that it is known
    again = ingest_tree(cfg, cfg.corpus_dir)
    assert again[0].status == "existing"
