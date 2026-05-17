"""Tests for ingest kind-inference, the propose pipeline (notes → draft
folder), and compose (notes + bibliography → article.tex).

No network or API: PhilArchive is the captured RSS fixture and Claude is
faked the same way the extraction/biblio tests fake it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import frontmatter
import pytest
from typer.testing import CliRunner

import cophilo.cli as cli_mod
from cophilo.cli import app
from cophilo.config import ensure_dirs, get_config
from cophilo.db import models as db
from cophilo.draft.compose import compose_draft
from cophilo.draft.propose import accept_proposal, collect_notes, propose_articles
from cophilo.draft.render_tex import render_tex, tex_escape
from cophilo.draft.schemas import (
    ArticleDraft,
    ArticleProposal,
    ArticleProposals,
    DraftSection,
)
from cophilo.extract.claude import ExtractionResult
from cophilo.ingest.dispatch import ingest_tree

FIXTURE = Path(__file__).parent / "fixtures" / "philarchive_free_will.rss"


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


class FakeClient:
    def __init__(self, parsed: Any):
        self.parsed = parsed
        self.calls: list[dict] = []

    def call(self, *, model, system, user, response_model, max_tokens):
        self.calls.append({"model": model, "system": system})
        return ExtractionResult(
            parsed=self.parsed,
            cache_read_tokens=1,
            cache_write_tokens=2,
            input_tokens=3,
            output_tokens=4,
        )


class FakeFetcher:
    def get(self, url: str) -> str:
        return FIXTURE.read_text(encoding="utf-8")


def _write_note(cfg, name: str, title: str, body: str) -> None:
    post = frontmatter.Post(body, title=title, kind="note", language="en")
    (cfg.corpus_notes_dir / name).write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8"
    )


# --- ingest kind inference / drafts skip ---------------------------------


def test_ingest_tree_infers_kind_and_skips_drafts(isolated_data_dir):
    cfg = isolated_data_dir
    _write_note(cfg, "n.md", "A note", "A passing thought on agency.")
    (cfg.corpus_articles_dir / "a.md").write_text(
        "# An Article\n\nA finished piece on free will.\n", encoding="utf-8"
    )
    drafted = cfg.corpus_drafts_dir / "wip"
    drafted.mkdir(parents=True)
    (drafted / "d.md").write_text("# Draft\n\nNot corpus material.\n", encoding="utf-8")

    outcomes = ingest_tree(cfg, cfg.corpus_dir)
    by_name = {o.path.name: o for o in outcomes}

    assert "d.md" not in by_name  # drafts excluded
    assert by_name["n.md"].kind == "note"
    assert by_name["a.md"].kind == "article"
    assert all(o.status == "new" for o in outcomes)


# --- propose -------------------------------------------------------------


def _ingest_two_notes(cfg) -> list[int]:
    _write_note(cfg, "note-a.md", "Frankfurt cases", "Frankfurt cases pressure PAP.")
    _write_note(cfg, "note-b.md", "Sourcehood", "Sourcehood views relocate the worry.")
    outcomes = ingest_tree(cfg, cfg.corpus_dir)
    return sorted(o.doc_id for o in outcomes)


def test_propose_filters_unknown_note_ids(isolated_data_dir):
    cfg = isolated_data_dir
    ids = _ingest_two_notes(cfg)
    proposal = ArticleProposal(
        slug="frankfurt-and-sourcehood",
        title="Frankfurt Cases and the Sourcehood Turn",
        thesis="PAP failure motivates sourcehood compatibilism.",
        rationale="Both notes track the same dialectic.",
        note_ids=[ids[0], ids[1], 9999],  # 9999 is hallucinated
        outline=["Intro", "Frankfurt", "Sourcehood", "Conclusion"],
        open_questions=["Does sourcehood collapse into leeway?"],
    )
    fake = FakeClient(ArticleProposals(proposals=[proposal]))
    result = propose_articles(cfg, client=fake, language="en")

    assert result.notes_considered == 2
    assert len(result.proposals) == 1
    assert set(result.proposals[0].note_ids) == set(ids)  # 9999 dropped
    assert fake.calls[0]["model"] == cfg.claude_model_hard


def test_propose_no_notes_skips_llm(isolated_data_dir):
    cfg = isolated_data_dir
    fake = FakeClient(ArticleProposals(proposals=[]))
    result = propose_articles(cfg, client=fake)
    assert result.proposals == [] and result.notes_considered == 0
    assert fake.calls == []  # no notes → no tokens spent


def test_accept_proposal_moves_notes_and_repoints(isolated_data_dir):
    cfg = isolated_data_dir
    ids = _ingest_two_notes(cfg)
    proposal = ArticleProposal(
        slug="frankfurt-and-sourcehood",
        title="Frankfurt Cases and the Sourcehood Turn",
        thesis="PAP failure motivates sourcehood compatibilism.",
        rationale="Both notes track the same dialectic.",
        note_ids=ids,
        outline=["Intro", "Conclusion"],
        open_questions=["Open?"],
    )
    fake = FakeClient(ArticleProposals(proposals=[proposal]))
    result = propose_articles(cfg, client=fake)

    draft_dir = accept_proposal(cfg, result.proposals[0], result.notes_by_id)

    assert draft_dir.parent == cfg.corpus_drafts_dir
    assert (draft_dir / "OUTLINE.md").exists()
    # original note files moved out of notes/
    assert not (cfg.corpus_notes_dir / "note-a.md").exists()
    assert (draft_dir / "note-a.md").exists()
    assert (draft_dir / "note-b.md").exists()

    # DB source_path repointed into the draft folder
    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT source_path FROM documents WHERE id IN (?, ?);", tuple(ids)
        ).fetchall()
    assert all(str(draft_dir) in r["source_path"] for r in rows)

    # collect_notes now excludes them (they live under drafts/)
    assert collect_notes(cfg) == []

    outline = frontmatter.loads((draft_dir / "OUTLINE.md").read_text())
    assert outline["kind"] == "draft"
    assert "Thesis" in outline.content


# --- compose -------------------------------------------------------------


def _canned_draft() -> ArticleDraft:
    return ArticleDraft(
        title="Frankfurt Cases & the Sourcehood Turn",
        abstract="We argue PAP failure motivates sourcehood views.",
        keywords=["free will", "PAP"],
        sections=[
            DraftSection(heading="Introduction", body="We begin with 50% & a $ sign."),
            DraftSection(heading="Conclusion", body="Therefore, sourcehood."),
        ],
        references=["List, C. (2014). Free Will, Determinism. Noûs."],
    )


def test_compose_draft_writes_tex_and_persists_biblio(isolated_data_dir):
    cfg = isolated_data_dir
    draft_dir = cfg.corpus_drafts_dir / "frankfurt-and-sourcehood"
    draft_dir.mkdir(parents=True)
    _note = frontmatter.Post(
        "Frankfurt cases pressure PAP; sourcehood relocates the worry.",
        title="Frankfurt notes",
        kind="note",
    )
    (draft_dir / "note-a.md").write_text(
        frontmatter.dumps(_note) + "\n", encoding="utf-8"
    )
    outline = frontmatter.Post(
        "## Thesis\n\nPAP failure motivates sourcehood.\n\n## Outline\n\n1. Intro\n",
        title="Frankfurt Cases and the Sourcehood Turn",
        kind="draft",
    )
    (draft_dir / "OUTLINE.md").write_text(
        frontmatter.dumps(outline) + "\n", encoding="utf-8"
    )

    fake = FakeClient(_canned_draft())
    result = compose_draft(
        cfg, draft_dir, client=fake, fetcher=FakeFetcher(), language="en"
    )

    assert result.tex_path == draft_dir / "article.tex"
    tex = result.tex_path.read_text(encoding="utf-8")
    assert r"\documentclass" in tex
    assert r"\section{Introduction}" in tex
    assert r"50\% \& a \$ sign" in tex  # LaTeX escaping
    assert r"\begin{thebibliography}" in tex
    # bibliography fixture persisted
    with sqlite3.connect(cfg.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM bibliography;").fetchone()[0]
    assert n == 3
    # thesis from OUTLINE.md fed into the prompt
    assert "PAP failure motivates sourcehood" in fake.calls[0]["system"]


def test_compose_rejects_empty_folder(isolated_data_dir):
    cfg = isolated_data_dir
    empty = cfg.corpus_drafts_dir / "empty"
    empty.mkdir(parents=True)
    with pytest.raises(ValueError):
        compose_draft(cfg, empty, client=FakeClient(_canned_draft()), fetcher=FakeFetcher())


def test_render_tex_escaping_unit():
    assert tex_escape("a_b & c% {d}") == r"a\_b \& c\% \{d\}"
    tex = render_tex(_canned_draft(), language="fr")
    assert r"\usepackage[french]{babel}" in tex
    assert tex.strip().endswith(r"\end{document}")


# --- CLI -----------------------------------------------------------------

runner = CliRunner()


def test_cli_propose_accept(monkeypatch, isolated_data_dir):
    cfg = isolated_data_dir
    ids = _ingest_two_notes(cfg)
    proposal = ArticleProposal(
        slug="frankfurt-and-sourcehood",
        title="Frankfurt Cases and the Sourcehood Turn",
        thesis="PAP failure motivates sourcehood.",
        rationale="Same dialectic.",
        note_ids=ids,
        outline=["Intro"],
        open_questions=[],
    )

    def fake_propose(cfg, **kw):
        from cophilo.draft.propose import ProposeResult

        notes = {n.doc_id: n for n in collect_notes(cfg)}
        return ProposeResult(
            proposals=[proposal], notes_by_id=notes, notes_considered=len(notes),
            input_tokens=3, output_tokens=4,
        )

    monkeypatch.setattr(cli_mod, "propose_articles", fake_propose)
    # confirm 'y', then accept default slug (blank → default)
    res = runner.invoke(app, ["propose"], input="y\n\n")
    assert res.exit_code == 0, res.output
    assert "created" in res.output
    assert (cfg.corpus_drafts_dir / "frankfurt-and-sourcehood").is_dir()


def test_cli_draft(monkeypatch, isolated_data_dir):
    cfg = isolated_data_dir
    draft_dir = cfg.corpus_drafts_dir / "x"
    draft_dir.mkdir(parents=True)

    from cophilo.draft.compose import ComposeResult

    def fake_compose(cfg, folder, **kw):
        return ComposeResult(
            draft=_canned_draft(),
            entries=[],
            tex_path=folder / "article.tex",
            query="free will",
            language="en",
            input_tokens=1,
            output_tokens=2,
        )

    monkeypatch.setattr(cli_mod, "compose_draft", fake_compose)
    res = runner.invoke(app, ["draft", "x"])
    assert res.exit_code == 0, res.output
    assert "Wrote" in res.output and "article.tex" in res.output
