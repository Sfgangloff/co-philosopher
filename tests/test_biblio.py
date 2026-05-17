"""Tests for the bibliography feature: PhilArchive parsing/search, the
Claude synthesis (mocked), DB persistence, and the CLI surface.

No network or API calls — the RSS feed is a captured fixture and Claude is
faked the same way the extraction tests fake it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cophilo.biblio import philarchive
from cophilo.biblio.philarchive import _parse_description, _parse_title, parse_feed, search_url
from cophilo.biblio.schemas import KeyWork, TopicSynthesis
from cophilo.biblio.synthesize import render_markdown, synthesize_topic
from cophilo.cli import app
from cophilo.config import ensure_dirs, get_config
from cophilo.db import models as db
from cophilo.extract.claude import ExtractionResult

FIXTURE = Path(__file__).parent / "fixtures" / "philarchive_free_will.rss"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    db_path = data_dir / "db" / "cophilo.sqlite"
    monkeypatch.setenv("COPHILO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COPHILO_DB_PATH", str(db_path))
    get_config.cache_clear()
    cfg = get_config()
    ensure_dirs(cfg)
    db.init_db(cfg)
    yield cfg
    get_config.cache_clear()


# --- parsing -------------------------------------------------------------


def test_parse_feed_fixture():
    entries = parse_feed(FIXTURE.read_text(encoding="utf-8"))
    assert len(entries) == 3

    by_id = {e.external_id: e for e in entries}
    e = by_id["LISFWD"]
    assert e.source == "philarchive"
    assert e.title == "Free Will, Determinism, and the Possibility of Doing Otherwise"
    assert e.authors == ["List, Christian"]
    assert e.journal == "Noûs"  # numeric char ref decoded
    assert e.year == 2014
    assert e.abstract and e.abstract.startswith("I argue that free will and determinism are compatible")
    assert e.url == "https://philarchive.org/rec/LISFWD"


def test_parse_title_edge_cases():
    assert _parse_title("List, Christian: A Title: With Colons") == (
        ["List, Christian"],
        "A Title: With Colons",
    )
    # multiple authors split on ';' and '&'
    authors, title = _parse_title("Doe, Jane ; Roe, R. & Smith, S.: Paper")
    assert authors == ["Doe, Jane", "Roe, R.", "Smith, S."]
    assert title == "Paper"
    # no "author: title" separator
    assert _parse_title("An Untitled Fragment") == ([], "An Untitled Fragment")


def test_parse_description_glued_year_and_html():
    journal, year, abstract = _parse_description(
        '_Journal of X_ 12 (3):1-20. 2020The core claim is made here.'
        '<div>(<a href="http://x">direct link</a>)</div>'
    )
    assert journal == "Journal of X"
    assert year == 2020
    assert abstract == "The core claim is made here."

    # preprint: no journal, no citation year → abstract preserved whole
    j, y, a = _parse_description("This is a working paper with no venue.")
    assert (j, y) == (None, None)
    assert a == "This is a working paper with no venue."


# --- search (no network) -------------------------------------------------


class FakeFetcher:
    def __init__(self, text: str):
        self._text = text
        self.url: str | None = None

    def get(self, url: str) -> str:
        self.url = url
        return self._text


def test_search_uses_fetcher_and_respects_limit(isolated_data_dir):
    cfg = isolated_data_dir
    fake = FakeFetcher(FIXTURE.read_text(encoding="utf-8"))
    entries = philarchive.search(cfg, "free will", limit=2, fetcher=fake)
    assert len(entries) == 2
    assert fake.url == search_url(cfg, "free will")
    assert "/s/free%20will?format=rss" in fake.url


def test_search_rejects_empty_query(isolated_data_dir):
    with pytest.raises(ValueError):
        philarchive.search(isolated_data_dir, "   ", fetcher=FakeFetcher(""))


# --- persistence ---------------------------------------------------------


def test_upsert_bibliography_is_idempotent(isolated_data_dir):
    cfg = isolated_data_dir
    entries = parse_feed(FIXTURE.read_text(encoding="utf-8"))
    with db.transaction(cfg) as conn:
        for e in entries:
            db.upsert_bibliography(
                conn,
                source=e.source,
                external_id=e.external_id,
                title=e.title,
                authors=e.authors_str() or None,
                journal=e.journal,
                year=e.year,
                abstract=e.abstract,
            )
    # re-insert the same records with a changed title → update, not duplicate
    with db.transaction(cfg) as conn:
        bid = db.upsert_bibliography(
            conn,
            source="philarchive",
            external_id="LISFWD",
            title="Updated Title",
            authors="List, Christian",
            journal="Noûs",
            year=2014,
            abstract="x",
        )
    with sqlite3.connect(cfg.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM bibliography;").fetchone()[0]
        title = conn.execute(
            "SELECT title FROM bibliography WHERE id = ?;", (bid,)
        ).fetchone()[0]
    assert n == 3
    assert title == "Updated Title"


# --- synthesis (mocked Claude) ------------------------------------------


class FakeClient:
    def __init__(self, parsed: Any):
        self.parsed = parsed
        self.calls: list[dict] = []

    def call(self, *, model, system, user, response_model, max_tokens):
        self.calls.append({"model": model, "system": system, "user": user})
        return ExtractionResult(
            parsed=self.parsed,
            cache_read_tokens=11,
            cache_write_tokens=22,
            input_tokens=33,
            output_tokens=44,
        )


def _canned_synthesis() -> TopicSynthesis:
    return TopicSynthesis(
        overview="The literature splits over compatibilism.",
        big_questions=["Is free will compatible with determinism?"],
        small_questions=["What does 'could have done otherwise' mean modally?"],
        key_works=[KeyWork(title="Free Will, Determinism…", authors="List", why="Sets the modal framing.")],
        suggested_searches=["leeway compatibilism", "sourcehood free will"],
    )


def test_synthesize_topic_with_fake_client(isolated_data_dir):
    cfg = isolated_data_dir
    entries = parse_feed(FIXTURE.read_text(encoding="utf-8"))
    fake = FakeClient(_canned_synthesis())
    result = synthesize_topic(
        cfg, "Whether the ability to do otherwise survives determinism.", entries, client=fake
    )
    assert result.synthesis.big_questions
    assert result.input_tokens == 33 and result.output_tokens == 44
    assert result.cache_read_tokens == 11 and result.cache_write_tokens == 22
    # the entries were rendered into the system prompt
    assert "Free Will, Determinism" in fake.calls[0]["system"]
    assert fake.calls[0]["model"] == cfg.claude_model_hard

    md = render_markdown("My topic.", "free will", result.synthesis, entries)
    assert "## Big questions" in md
    assert "Is free will compatible with determinism?" in md
    assert "## Key works" in md
    assert "philarchive.org/rec/LISFWD" in md


def test_synthesize_topic_rejects_empty_topic(isolated_data_dir):
    with pytest.raises(ValueError):
        synthesize_topic(isolated_data_dir, "  ", [], client=FakeClient(_canned_synthesis()))


# --- CLI -----------------------------------------------------------------

runner = CliRunner()


def _entries():
    return parse_feed(FIXTURE.read_text(encoding="utf-8"))


def test_cli_biblio_search_json(monkeypatch, isolated_data_dir):
    monkeypatch.setattr(philarchive, "search", lambda cfg, q, **kw: _entries()[: kw.get("limit", 25)])
    res = runner.invoke(app, ["biblio", "search", "free will", "--json", "--no-save"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert {e["external_id"] for e in payload} == {"MORATA-16", "LISFWD", "MOOLFW"}


def test_cli_biblio_search_persists(monkeypatch, isolated_data_dir):
    cfg = isolated_data_dir
    monkeypatch.setattr(philarchive, "search", lambda cfg, q, **kw: _entries())
    res = runner.invoke(app, ["biblio", "search", "free will"])
    assert res.exit_code == 0, res.output
    with sqlite3.connect(cfg.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM bibliography;").fetchone()[0]
    assert n == 3


def test_cli_biblio_synthesize(monkeypatch, isolated_data_dir, tmp_path):
    import cophilo.cli as cli_mod
    from cophilo.biblio.synthesize import SynthesisResult

    monkeypatch.setattr(philarchive, "search", lambda cfg, q, **kw: _entries())

    def fake_synth(cfg, topic, entries, **kw):
        return SynthesisResult(
            synthesis=_canned_synthesis(),
            entries=entries,
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )

    monkeypatch.setattr(cli_mod, "synthesize_topic", fake_synth)

    out = tmp_path / "synthesis.md"
    res = runner.invoke(
        app,
        ["biblio", "synthesize", "--topic", "Doing otherwise under determinism", "--out", str(out), "--no-save"],
    )
    assert res.exit_code == 0, res.output
    assert "## Big questions" in res.stdout
    assert "Is free will compatible with determinism?" in res.stdout
    assert out.exists() and "## Overview" in out.read_text(encoding="utf-8")


def test_cli_biblio_synthesize_needs_topic(isolated_data_dir):
    res = runner.invoke(app, ["biblio", "synthesize"])
    assert res.exit_code != 0
