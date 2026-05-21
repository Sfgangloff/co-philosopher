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


def test_parse_description_long_metadata_chapter_with_editors():
    """§4.4 (case 1) — book chapter with editors pushes the citation year
    past offset 60. The year is still at a clean ". <year><Uppercase>"
    boundary; cut it instead of leaving "2018Forgetting…" in the abstract."""
    j, y, a = _parse_description(
        "_Forgetting_, K. Michaelian, D. Debus & D. Perrin (eds.), "
        "Routledge, pp. 223-240. 2018Forgetting is importantly related to memory."
    )
    assert j == "Forgetting"
    assert y == 2018
    assert a == "Forgetting is importantly related to memory."


def test_parse_description_parenthetical_intext_citation_kept_with_space():
    """§4.4 (case 2) — "(Cuc, Koppel, & Hirst, 2007This…)" is an in-text
    citation INSIDE the abstract (preceded by comma, not period), so the
    year must NOT be cut. But the glued capital is a typo we can cosmetically
    fix by inserting a space."""
    j, y, a = _parse_description(
        "This study replicates earlier work (Cuc, Koppel, & Hirst, 2007This experiment investigated…) and goes further."
    )
    assert j is None
    # Year was inside prose; not stripped as a citation year:
    assert y is None
    # And the glued "2007This" was cosmetically fixed:
    assert a is not None and "2007 This experiment" in a
    assert "2007This" not in a


def test_parse_description_forthcoming_and_glued_year_no_journal():
    # "forthcoming" glued to the abstract, with a journal prefix.
    j, y, a = _parse_description("_Synthese_ forthcomingThis paper argues X.")
    assert j == "Synthese"
    assert y is None
    assert a == "This paper argues X."

    # Year glued to a capitalised first word, with NO journal prefix: the
    # 4-digit-then-capital boundary is enough to split the citation off.
    j, y, a = _parse_description("Philosophical Review 134. 2025In this chapter I argue.")
    assert y == 2025
    assert a == "In this chapter I argue."

    # "in press" with no journal but at the very start.
    j, y, a = _parse_description("in press. The central thesis is defended here.")
    assert (j, y) == (None, None)
    assert a == "The central thesis is defended here."


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
    # §1.2 — synthesis is persisted to data/syntheses/<slug>.{json,md}
    # whether or not --out was passed.
    cfg = isolated_data_dir
    saved = list(cfg.syntheses_dir.glob("*.json"))
    assert len(saved) == 1, f"expected one saved synthesis, got {saved}"
    saved_md = list(cfg.syntheses_dir.glob("*.md"))
    assert len(saved_md) == 1
    # The user-friendly echo mentions the saved path.
    assert "Saved synthesis" in res.output or "Saved synthesis" in (res.stderr or "")


def test_synthesis_save_and_load_roundtrip(isolated_data_dir):
    """§1.2 — a saved synthesis round-trips through load_synthesis with the
    structured fields (source_judgements, missing_canonical, corpus_caveats)
    preserved, so `draft --from-synthesis` can consume them."""
    from cophilo.biblio.schemas import MissingCanonical, SourceJudgement
    from cophilo.biblio.synthesize import (
        load_synthesis,
        save_synthesis,
        synthesis_paths,
    )

    cfg = isolated_data_dir
    entries = parse_feed(FIXTURE.read_text(encoding="utf-8"))
    synthesis = TopicSynthesis(
        overview="An overview.",
        big_questions=["Q1?"],
        small_questions=[],
        key_works=[],
        suggested_searches=["follow-up"],
        source_judgements=[
            SourceJudgement(
                external_id="LISFWD",
                tier="peer_reviewed",
                rationale="Published in Noûs.",
                cite_as="primary",
            ),
            SourceJudgement(
                external_id="MORATA-16",
                tier="speculative",
                rationale="Single-author repeat.",
                cite_as="do_not_cite",
            ),
        ],
        missing_canonical=[
            MissingCanonical(
                author="Fischer, J. M.",
                work_hint="semicompatibilism",
                why="Standard reference omitted.",
            ),
        ],
        corpus_caveats="Corpus is thin on the empirical side.",
    )
    json_path, md_path = save_synthesis(
        cfg, "Doing otherwise", "free will", synthesis, entries
    )
    assert json_path.exists() and md_path.exists()
    # Round-trip:
    loaded = load_synthesis(json_path)
    assert loaded.topic == "Doing otherwise"
    assert loaded.query == "free will"
    assert {e.external_id for e in loaded.entries} == {e.external_id for e in entries}
    assert loaded.synthesis.corpus_caveats == "Corpus is thin on the empirical side."
    judgements = {j.external_id: j for j in loaded.synthesis.source_judgements}
    assert judgements["LISFWD"].tier == "peer_reviewed"
    assert judgements["MORATA-16"].cite_as == "do_not_cite"
    assert loaded.synthesis.missing_canonical[0].author == "Fischer, J. M."

    # synthesis_paths is deterministic on the topic (same topic → same path).
    again_json, again_md = synthesis_paths(cfg, "Doing otherwise")
    assert again_json == json_path and again_md == md_path

    # And the rendered MD surfaces the new sections:
    md = md_path.read_text(encoding="utf-8")
    assert "## Corpus caveats" in md
    assert "## Missing canonical literature" in md
    assert "## Source-quality verdicts" in md
    assert "[peer_reviewed]" in md
    # Suggested follow-up searches are now runnable commands, not bare backticks.
    assert "cophilo biblio search" in md


def test_synthesize_cross_links_to_candidate_venues(isolated_data_dir):
    """§2.3 — saved synthesis carries candidate venues (memory.search), and
    the rendered MD surfaces them under `## Candidate venues`. If the
    memory index isn't built, the lookup silently no-ops."""
    cfg = isolated_data_dir
    entries = parse_feed(FIXTURE.read_text(encoding="utf-8"))
    venues = [
        {
            "name": "Mind & Language",
            "score": 0.78,
            "open_access": False,
            "scope": "analytic phil of mind / language",
            "url": "https://onlinelibrary.wiley.com/journal/14680017",
        },
        {
            "name": "Synthese",
            "score": 0.71,
            "open_access": False,
            "scope": "philosophy of science / language",
            "url": "",
        },
    ]
    from cophilo.biblio.synthesize import load_synthesis, save_synthesis
    json_path, md_path = save_synthesis(
        cfg,
        "philosophy of memory, forgetting, extended mind",
        "memory forgetting",
        _canned_synthesis(),
        entries,
        venues=venues,
    )
    md = md_path.read_text(encoding="utf-8")
    assert "## Candidate venues" in md
    assert "Mind & Language" in md
    assert "score 0.78" in md
    # Round-trips through load_synthesis:
    loaded = load_synthesis(json_path)
    assert len(loaded.venues) == 2
    assert loaded.venues[0]["name"] == "Mind & Language"


def test_synthesize_venue_lookup_no_op_when_index_missing(isolated_data_dir):
    """The cross-link is best-effort: if `memory.search` raises
    FileNotFoundError (no index built), `candidate_venues` returns [] and
    `save_synthesis` keeps working without it."""
    from cophilo.biblio.synthesize import (
        candidate_venues,
        load_synthesis,
        save_synthesis,
    )
    cfg = isolated_data_dir
    # The fixture has no journals.yaml or memory.sqlite, so the search must
    # fail soft. candidate_venues returns [] without raising.
    assert candidate_venues(cfg, "any topic") == []
    json_path, md_path = save_synthesis(
        cfg, "topic", "query", _canned_synthesis(),
        parse_feed(FIXTURE.read_text(encoding="utf-8")),
    )
    md = md_path.read_text(encoding="utf-8")
    # No venues → no Candidate venues section is rendered.
    assert "## Candidate venues" not in md
    # JSON has a venues key but it's empty.
    loaded = load_synthesis(json_path)
    assert loaded.venues == []


def test_cli_biblio_synthesize_needs_topic(isolated_data_dir):
    res = runner.invoke(app, ["biblio", "synthesize"])
    assert res.exit_code != 0
