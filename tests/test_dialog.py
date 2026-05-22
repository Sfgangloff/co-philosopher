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
            "Boredom is not a mere absence;",
            "it has its own intentional structure.",
            "",  # blank line commits the multi-line note
            "A second, separate thought.",
            "",  # commits — appended to the same session file
            "/new",  # boundary: the next note starts a fresh file
            "a throwaway draft line",
            "/cancel",  # discards the in-progress (uncommitted) note
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
    # One session = one file; the /new boundary's note was cancelled.
    assert len(session.files) == 1
    assert session.files[0].exists()
    assert session.total_entries == 2  # two committed notes

    body = frontmatter.loads(
        session.files[0].read_text(encoding="utf-8")
    ).content
    # Multi-line input is preserved as one note, newline kept inside it.
    assert "Boredom is not a mere absence;\nit has its own intentional structure." in body
    assert "A second, separate thought." in body
    assert "throwaway draft line" not in body  # cancelled, never written

    assert "Saved" in session.summary()
    assert any("discarded" in line for line in out)


def test_pending_note_survives_done(isolated_data_dir):
    cfg = isolated_data_dir
    script = iter(["An unfinished but real thought", "/done"])
    session = run_dialog(
        cfg,
        input_fn=lambda _prompt: next(script),
        echo=lambda _msg: None,
        clock=_CLOCK,
    )
    # No blank line before /done, but the pending note is committed, not lost.
    assert session.total_entries == 1
    assert len(session.files) == 1
    body = frontmatter.loads(
        session.files[0].read_text(encoding="utf-8")
    ).content
    assert "An unfinished but real thought" in body


@pytest.mark.parametrize("word", ["exit", "quit", "done", "EXIT", "Quit"])
def test_bare_exit_word_leaves_when_buffer_empty(isolated_data_dir, word):
    """§4.1 — `exit` outside / `/done` inside dialog is a muscle-memory trap.
    A single-word `exit`/`quit`/`done` (no slash) on its own line, between
    notes, should leave instead of being saved as note text."""
    cfg = isolated_data_dir
    script = iter(["A real thought.", "", word])  # commit, then bare 'exit'
    out: list[str] = []
    session = run_dialog(
        cfg,
        input_fn=lambda _prompt: next(script),
        echo=out.append,
        clock=_CLOCK,
    )
    assert session.total_entries == 1
    body = frontmatter.loads(
        session.files[0].read_text(encoding="utf-8")
    ).content
    assert word.lower() not in body.lower()  # the stray word is NOT in the note
    assert any("leaving" in line for line in out)


def test_bare_exit_word_prompts_when_buffer_nonempty(isolated_data_dir):
    """When mid-note, `exit` alone should NOT leave and should NOT silently
    join the note as text — it should disambiguate."""
    cfg = isolated_data_dir
    script = iter([
        "Line one of an argument.",
        "exit",       # mid-note: ambiguous, must prompt
        "still going",
        "",           # commit
        "/done",
    ])
    out: list[str] = []
    session = run_dialog(
        cfg,
        input_fn=lambda _prompt: next(script),
        echo=out.append,
        clock=_CLOCK,
    )
    assert session.total_entries == 1
    body = frontmatter.loads(
        session.files[0].read_text(encoding="utf-8")
    ).content
    assert "Line one" in body and "still going" in body
    assert "exit" not in body  # not absorbed as note text
    assert any("type /done to leave" in line for line in out)


def test_appended_notes_have_single_blank_line_separator(isolated_data_dir):
    """§4.7 — appending must yield exactly one blank line between notes,
    never a \\n\\n\\n run."""
    cfg = isolated_data_dir
    s = DialogSession(cfg=cfg, topic="forgetting", clock=_CLOCK)
    s.add("First note paragraph one.")
    s.add("Second committed note.")
    s.add("Third committed note.")
    raw = s.files[0].read_text(encoding="utf-8")
    body = frontmatter.loads(raw).content
    # No triple-newline runs:
    assert "\n\n\n" not in body
    # And the three notes are all there, separated by exactly one blank line:
    assert "First note paragraph one.\n\nSecond committed note.\n\nThird committed note." in body


# --- §3 opt-in socratic interlocutor ---------------------------------------


class _FakeClient:
    """Records `.call(...)` invocations and returns a canned SocraticQuestion."""

    def __init__(self, question_text: str = "What grounds the modal claim?"):
        from cophilo.extract.claude import ExtractionResult
        from cophilo.notes.schemas import SocraticQuestion

        self._parsed = SocraticQuestion(question=question_text)
        self._ExtractionResult = ExtractionResult
        self.calls: list[dict] = []

    def call(self, *, model, system, user, response_model, max_tokens):
        self.calls.append({"system": system, "user": user, "model": model})
        return self._ExtractionResult(
            parsed=self._parsed,
            cache_read_tokens=1,
            cache_write_tokens=2,
            input_tokens=3,
            output_tokens=4,
        )


def test_socratic_question_helper_calls_claude_and_returns_parsed(isolated_data_dir):
    """`socratic_question` shapes the call (system prompt with language;
    user message is the note) and returns the parsed pydantic model."""
    from cophilo.notes.capture import socratic_question

    fake = _FakeClient("Are you smuggling 'memory' for 'recall'?")
    q = socratic_question(
        isolated_data_dir,
        "Forgetting is constitutive of concept-formation.",
        language="en",
        client=fake,
    )
    assert q.question == "Are you smuggling 'memory' for 'recall'?"
    assert len(fake.calls) == 1
    sys = fake.calls[0]["system"]
    user = fake.calls[0]["user"]
    # Prompt makes the "no summary / no affirmation" contract explicit:
    assert "do not produce" in sys.lower() or "à ne pas produire" in sys.lower()
    assert "Forgetting is constitutive" in user
    # Routine (cheap) model is used for the per-commit ping:
    assert fake.calls[0]["model"] == isolated_data_dir.claude_model_routine


def test_run_dialog_socratic_echoes_after_each_commit_and_does_not_save_question(
    isolated_data_dir,
):
    """§3 — with `socratic=True`, the REPL echoes ONE question after each
    committed note (one LLM call per commit) and the question never reaches
    the note file on disk."""
    cfg = isolated_data_dir
    fake = _FakeClient("Are you smuggling 'memory' for 'recall'?")
    script = iter(
        [
            "Forgetting is constitutive of concept-formation.",
            "",  # commit → expect a socratic question
            "Funes is the limit case.",
            "",  # commit → expect a second socratic question
            "/done",
        ]
    )
    out: list[str] = []
    session = run_dialog(
        cfg,
        socratic=True,
        client=fake,
        input_fn=lambda _p: next(script),
        echo=out.append,
        clock=_CLOCK,
    )

    # Two commits → two API calls; one question echoed per commit.
    assert session.total_entries == 2
    assert len(fake.calls) == 2
    questions_echoed = [line for line in out if line.lstrip().startswith("?  ")]
    assert len(questions_echoed) == 2
    assert all("memory" in q for q in questions_echoed)
    # Up-front banner warns that this mode bills:
    assert any("socratic mode" in line for line in out)

    # And the question is NOT written into the note file on disk:
    body = frontmatter.loads(
        session.files[0].read_text(encoding="utf-8")
    ).content
    assert "smuggling" not in body
    assert "Forgetting is constitutive" in body and "Funes is the limit case" in body


def test_run_dialog_socratic_disabled_when_client_construction_fails(
    isolated_data_dir, monkeypatch
):
    """If `make_client` cannot be built (no API key / no `claude` CLI), the
    REPL falls back to offline mode and tells the user — never a runtime
    explosion mid-session."""
    from cophilo import notes  # ensure namespace import order
    from cophilo.extract import claude as claude_mod

    def boom(_cfg):
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    monkeypatch.setattr(claude_mod, "make_client", boom)
    script = iter(["A note.", "", "/done"])
    out: list[str] = []
    session = run_dialog(
        isolated_data_dir,
        socratic=True,
        input_fn=lambda _p: next(script),
        echo=out.append,
        clock=_CLOCK,
    )
    assert session.total_entries == 1
    assert any("socratic disabled" in line for line in out)
    # And no `?` question was echoed:
    assert not any(line.lstrip().startswith("?  ") for line in out)


def test_run_dialog_socratic_off_by_default_no_llm_calls(isolated_data_dir):
    """The default REPL stays offline. Passing a client without `socratic=True`
    must not result in any LLM calls — the offline-default invariant."""
    fake = _FakeClient()
    script = iter(["A note.", "", "/done"])
    out: list[str] = []
    run_dialog(
        isolated_data_dir,
        client=fake,
        input_fn=lambda _p: next(script),
        echo=out.append,
        clock=_CLOCK,
    )
    assert fake.calls == []
    assert not any("socratic mode" in line for line in out)


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
