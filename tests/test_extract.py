"""End-to-end test for the extraction pipeline with a mocked Claude client.

Covers:
  - segmentation of a normalized markdown document
  - concept-pass: existing concepts → mentions, new concepts → review queue
  - question-pass: creates question + question_mention rows
  - external-author attribution
  - merged-concept resolution
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from cophilo.config import ensure_dirs, get_config
from cophilo.db import models as db
from cophilo.extract.claude import ExtractionResult
from cophilo.extract.passes import extract_document
from cophilo.extract.schemas import (
    ConceptMention,
    ConceptPassResponse,
    QuestionMention,
    QuestionPassResponse,
)
from cophilo.extract.segment import segment


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


# --- segmentation -------------------------------------------------------


def test_segment_paragraphs_and_sections():
    body = (
        "# Introduction\n\n"
        "This is the first paragraph of the introduction.\n"
        "It spans two lines.\n\n"
        "## A subsection\n\n"
        "<!-- page 2 -->\n\n"
        "Second paragraph under a subsection.\n"
    )
    passages = segment(body)
    assert len(passages) == 2
    assert passages[0].section_path == "Introduction"
    assert "first paragraph" in passages[0].text
    assert passages[1].section_path == "Introduction/A subsection"
    assert "Second paragraph" in passages[1].text
    # char offsets resolve back into the body
    for p in passages:
        assert body[p.char_start:p.char_end].strip() == p.text


# --- mock client --------------------------------------------------------


class FakeClient:
    """An LLMClient that returns canned ConceptPassResponse / QuestionPassResponse."""

    def __init__(self, responses: dict[type, Any]):
        self.responses = responses
        self.calls: list[dict] = []

    def call(self, *, model, system, user, response_model, max_tokens):
        self.calls.append({"model": model, "system": system, "response_model": response_model})
        if response_model not in self.responses:
            raise AssertionError(f"unexpected response_model {response_model}")
        parsed = self.responses[response_model]
        return ExtractionResult(
            parsed=parsed,
            cache_read_tokens=100,
            cache_write_tokens=200,
            input_tokens=300,
            output_tokens=50,
        )


# --- helpers ------------------------------------------------------------


def _seed_normalized_doc(cfg, *, title: str, language: str, body: str) -> int:
    """Insert a documents row pointing at a normalized .md file we just wrote."""
    cfg.normalized_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.normalized_dir / "0001-test.md"
    path.write_text(
        f"---\ntitle: {title}\nsource: /tmp/source.tex\nsource_format: tex\n"
        f"language: {language}\n---\n{body}",
        encoding="utf-8",
    )
    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.execute(
            """
            INSERT INTO documents
                (kind, title, source_path, normalized_path, language, status, ingested_at, metadata_json)
            VALUES ('article', ?, '/tmp/source.tex', ?, ?, 'ingested', '2026-05-12T00:00:00+00:00', NULL);
            """,
            (title, str(path), language),
        )
        conn.commit()
        return int(cur.lastrowid)


def _seed_concept(cfg, *, slug: str, label_en: str, label_fr: str, description: str) -> int:
    with sqlite3.connect(cfg.db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO concepts (slug, canonical_label_en, canonical_label_fr, description,
                                  kind, status, created_at, confirmed_at)
            VALUES (?, ?, ?, ?, 'mine', 'confirmed',
                    '2026-05-12T00:00:00+00:00', '2026-05-12T00:00:00+00:00');
            """,
            (slug, label_en, label_fr, description),
        )
        conn.commit()
        return int(cur.lastrowid)


# --- end-to-end ---------------------------------------------------------


def test_extract_end_to_end(isolated_data_dir):
    cfg = isolated_data_dir
    body = (
        "# On Free Will\n\n"
        "The question of free will has been with us for centuries.\n"
        "It rests on the assumption of agency.\n\n"
        "## Husserl on intentionality\n\n"
        "As Husserl notes, intentionality is the directedness of consciousness.\n"
    )
    doc_id = _seed_normalized_doc(cfg, title="On Free Will", language="en", body=body)
    free_will_id = _seed_concept(
        cfg,
        slug="free-will",
        label_en="free will",
        label_fr="libre arbitre",
        description="The capacity to choose between alternatives.",
    )

    fake = FakeClient({
        ConceptPassResponse: ConceptPassResponse(
            mentions=[
                # existing concept mention
                ConceptMention(
                    passage_ord=1,
                    slug="free-will",
                    is_new=False,
                    role="introduce",
                    span_quote="free will has been with us for centuries",
                    confidence=0.95,
                    attributed_authors=[],
                ),
                # new-concept proposal with author attribution
                ConceptMention(
                    passage_ord=2,
                    is_new=True,
                    proposed_canonical_label_en="intentionality",
                    proposed_canonical_label_fr="intentionnalité",
                    proposed_description="Directedness of consciousness toward an object.",
                    role="define",
                    span_quote="intentionality is the directedness of consciousness",
                    confidence=0.9,
                    attributed_authors=["Husserl"],
                ),
                # under-confidence mention should be dropped
                ConceptMention(
                    passage_ord=1,
                    slug="free-will",
                    is_new=False,
                    role="use",
                    span_quote="agency",
                    confidence=0.3,
                ),
            ]
        ),
        QuestionPassResponse: QuestionPassResponse(
            questions=[
                QuestionMention(
                    passage_ord=1,
                    label="What grounds free will?",
                    description="What is the metaphysical basis for free will?",
                    role="raise",
                    explicit=True,
                    span_quote="The question of free will has been with us for centuries",
                    confidence=0.85,
                ),
            ]
        ),
    })

    stats = extract_document(cfg, doc_id, client=fake)

    assert stats.confirmed_mentions == 1
    assert stats.new_concept_proposals == 1
    assert stats.new_concept_labels == 1  # one distinct label among the proposals
    assert stats.question_mentions == 1
    assert stats.new_questions == 1
    # both passes ran
    assert len(fake.calls) == 2

    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row

        passages = conn.execute(
            "SELECT * FROM passages WHERE document_id = ? ORDER BY ord;", (doc_id,)
        ).fetchall()
        assert len(passages) == 2

        # confirmed mention with resolved span
        mention = conn.execute(
            "SELECT * FROM concept_mentions WHERE concept_id = ?;", (free_will_id,)
        ).fetchone()
        assert mention is not None
        assert mention["role"] == "introduce"
        # span back-resolved to non-null offsets
        assert mention["span_start"] is not None
        assert mention["span_end"] is not None
        passage_text = passages[0]["text"]
        assert (
            passage_text[mention["span_start"]:mention["span_end"]]
            == "free will has been with us for centuries"
        )

        # new concept queued for review (with author)
        review = conn.execute(
            "SELECT * FROM review_queue WHERE kind = 'new_concept';"
        ).fetchall()
        assert len(review) == 1
        import json
        payload = json.loads(review[0]["payload_json"])
        assert payload["proposed_canonical_label_en"] == "intentionality"
        assert payload["attributed_authors"] == ["Husserl"]

        # question + mention persisted
        questions = conn.execute("SELECT * FROM questions;").fetchall()
        assert len(questions) == 1
        assert questions[0]["label"] == "What grounds free will?"
        qms = conn.execute("SELECT * FROM question_mentions;").fetchall()
        assert len(qms) == 1
        assert qms[0]["role"] == "raise"

        # status flipped
        doc_row = conn.execute("SELECT status FROM documents WHERE id = ?;", (doc_id,)).fetchone()
        assert doc_row["status"] == "extracted"


def test_extract_resolves_merged_concept(isolated_data_dir):
    cfg = isolated_data_dir
    body = "# T\n\nA passage about the absolute.\n"
    doc_id = _seed_normalized_doc(cfg, title="T", language="en", body=body)
    target = _seed_concept(cfg, slug="absolute-spirit", label_en="absolute spirit",
                           label_fr="esprit absolu", description="Hegelian totality.")
    # An older slug merged into the canonical one
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute(
            """
            INSERT INTO concepts (slug, canonical_label_en, canonical_label_fr, description,
                                  kind, status, merged_into, created_at)
            VALUES ('the-absolute', 'the absolute', 'l''absolu', '', 'mine', 'merged',
                    ?, '2026-05-12T00:00:00+00:00');
            """,
            (target,),
        )
        conn.commit()

    fake = FakeClient({
        ConceptPassResponse: ConceptPassResponse(mentions=[
            ConceptMention(
                passage_ord=1,
                slug="the-absolute",  # resolves through merged_into
                is_new=False,
                role="use",
                span_quote="absolute",
                confidence=0.9,
            ),
        ]),
        QuestionPassResponse: QuestionPassResponse(questions=[]),
    })
    extract_document(cfg, doc_id, client=fake)

    with sqlite3.connect(cfg.db_path) as conn:
        rows = conn.execute("SELECT concept_id FROM concept_mentions;").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == target  # routed through merge


def test_extract_stats_track_mentions_and_distinct_labels(isolated_data_dir):
    """§4.3 — extract logs `N proposals queued`; concepts --pending grouped by
    label shows fewer rows. The two counts mean different things; stats must
    expose both so the CLI can disambiguate."""
    cfg = isolated_data_dir
    body = (
        "# T\n\n"
        "First passage talks about funesian exocortex.\n\n"
        "## S\n\n"
        "Second passage also about funesian exocortex.\n\n"
        "Third passage introduces a separate idea: plastic forgetting.\n"
    )
    doc_id = _seed_normalized_doc(cfg, title="T", language="en", body=body)
    fake = FakeClient({
        ConceptPassResponse: ConceptPassResponse(mentions=[
            ConceptMention(
                passage_ord=1, is_new=True,
                proposed_canonical_label_en="Funesian exocortex",
                proposed_description="A total-capture external memory.",
                role="define", span_quote="funesian exocortex", confidence=0.9,
            ),
            ConceptMention(
                passage_ord=2, is_new=True,
                proposed_canonical_label_en="Funesian exocortex",  # same label, different passage
                proposed_description="A total-capture external memory.",
                role="use", span_quote="funesian exocortex", confidence=0.9,
            ),
            ConceptMention(
                passage_ord=3, is_new=True,
                proposed_canonical_label_en="Plastic forgetting",
                proposed_description="Nietzsche's active forgetting.",
                role="introduce", span_quote="plastic forgetting", confidence=0.9,
            ),
        ]),
        QuestionPassResponse: QuestionPassResponse(questions=[]),
    })
    stats = extract_document(cfg, doc_id, client=fake)
    # Three proposal mentions queued (one per passage) but only two distinct
    # labels — the philosopher's "9 vs 7" discrepancy in miniature.
    assert stats.new_concept_proposals == 3
    assert stats.new_concept_labels == 2


def test_unknown_slug_falls_through_to_review(isolated_data_dir):
    """A model-claimed slug that doesn't exist should not silently drop;
    it should land in the review queue so a human can decide."""
    cfg = isolated_data_dir
    body = "# T\n\nA passage about phenomenology.\n"
    doc_id = _seed_normalized_doc(cfg, title="T", language="en", body=body)

    fake = FakeClient({
        ConceptPassResponse: ConceptPassResponse(mentions=[
            ConceptMention(
                passage_ord=1,
                slug="phenomenology",  # not in DB
                is_new=False,
                role="use",
                span_quote="phenomenology",
                confidence=0.8,
            ),
        ]),
        QuestionPassResponse: QuestionPassResponse(questions=[]),
    })
    stats = extract_document(cfg, doc_id, client=fake)
    assert stats.confirmed_mentions == 0
    assert stats.new_concept_proposals == 1

    with sqlite3.connect(cfg.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM review_queue WHERE kind='new_concept';").fetchone()[0]
        assert n == 1


# --- CLI: extract guards (B2/B4) and concepts/questions (P3) -------------

from typer.testing import CliRunner  # noqa: E402

import cophilo.cli as cli_mod  # noqa: E402
from cophilo.cli import app  # noqa: E402

_runner = CliRunner()


def _set_status(cfg, doc_id: int, status: str) -> None:
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute("UPDATE documents SET status=? WHERE id=?;", (status, doc_id))
        conn.commit()


def test_cli_extract_skips_extracted_unless_force(isolated_data_dir, monkeypatch):
    cfg = isolated_data_dir
    doc_id = _seed_normalized_doc(cfg, title="T", language="en", body="# T\n\nText.\n")
    _set_status(cfg, doc_id, "extracted")

    from cophilo.extract.passes import PassStats

    calls: list[int] = []

    def stub(c, did, **kw):
        calls.append(did)
        return PassStats(document_id=did)

    monkeypatch.setattr(cli_mod, "extract_document", stub)

    res = _runner.invoke(app, ["extract", "--doc", str(doc_id)])
    assert res.exit_code == 0, res.output
    assert "already extracted" in res.output
    assert calls == []  # no billed call

    res = _runner.invoke(app, ["extract", "--doc", str(doc_id), "--force"])
    assert res.exit_code == 0, res.output
    assert calls == [doc_id]  # --force re-runs


def test_cli_extract_unknown_doc_is_clean_error(isolated_data_dir):
    res = _runner.invoke(app, ["extract", "--doc", "999"])
    assert res.exit_code == 1
    assert "No document #999" in res.output
    assert "Traceback" not in res.output  # no raw traceback leaks


def test_cli_concepts_and_questions(isolated_data_dir):
    cfg = isolated_data_dir
    doc_id = _seed_normalized_doc(
        cfg, title="On Boredom", language="en", body="# B\n\nBoredom is intentional.\n"
    )
    cid = _seed_concept(
        cfg,
        slug="boredom",
        label_en="boredom",
        label_fr="ennui",
        description="An epistemic mood.",
    )
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute(
            "INSERT INTO passages (document_id, ord, char_start, char_end, "
            "section_path, text) VALUES (?, 1, 0, 5, '', 'Boredom is intentional.');",
            (doc_id,),
        )
        pid = conn.execute(
            "SELECT id FROM passages WHERE document_id=?;", (doc_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO concept_mentions (concept_id, passage_id, role, confidence) "
            "VALUES (?, ?, 'use', 0.9);",
            (cid, pid),
        )
        conn.execute(
            "INSERT INTO questions (label, description, status, first_raised_passage_id) "
            "VALUES ('Is boredom intentional?', 'A question about moods.', 'open', ?);",
            (pid,),
        )
        qid = conn.execute("SELECT id FROM questions;").fetchone()[0]
        conn.execute(
            "INSERT INTO question_mentions (question_id, passage_id, role) "
            "VALUES (?, ?, 'raise');",
            (qid, pid),
        )
        conn.commit()

    res = _runner.invoke(app, ["concepts"])
    assert res.exit_code == 0, res.output
    assert "boredom" in res.output and "1×" in res.output

    res = _runner.invoke(app, ["questions", "--doc", str(doc_id)])
    assert res.exit_code == 0, res.output
    assert "Is boredom intentional?" in res.output

    res = _runner.invoke(app, ["concepts", "--json"])
    assert res.exit_code == 0
    assert '"confirmed"' in res.output


def test_cli_concepts_json_uniform_name_key(isolated_data_dir):
    """§4.2 — `concepts --json` must expose a `name` key on BOTH confirmed
    and proposed items so integrators (the --json-for-tooling promise) can
    iterate without branching."""
    import json
    cfg = isolated_data_dir
    doc_id = _seed_normalized_doc(
        cfg, title="On Boredom", language="en", body="# B\n\nBoredom is intentional.\n"
    )
    cid = _seed_concept(
        cfg,
        slug="boredom",
        label_en="boredom",
        label_fr="ennui",
        description="An epistemic mood.",
    )
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute(
            "INSERT INTO passages (document_id, ord, char_start, char_end, "
            "section_path, text) VALUES (?, 1, 0, 5, '', 'Boredom is intentional.');",
            (doc_id,),
        )
        pid = conn.execute(
            "SELECT id FROM passages WHERE document_id=?;", (doc_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO concept_mentions (concept_id, passage_id, role, confidence) "
            "VALUES (?, ?, 'use', 0.9);",
            (cid, pid),
        )
        # Two proposed-new-concept review_queue entries for the same label —
        # so the grouped output should have count=2 for one unique label.
        for _ in range(2):
            conn.execute(
                "INSERT INTO review_queue (kind, payload_json, created_at) "
                "VALUES ('new_concept', ?, '2026-05-12T00:00:00+00:00');",
                (json.dumps({
                    "document_id": doc_id,
                    "passage_id": pid,
                    "proposed_canonical_label_en": "intentional mood",
                    "proposed_canonical_label_fr": "humeur intentionnelle",
                    "proposed_description": "A mood directed at the world.",
                }),),
            )
        # A third proposal with a different label to keep the multi-label shape.
        conn.execute(
            "INSERT INTO review_queue (kind, payload_json, created_at) "
            "VALUES ('new_concept', ?, '2026-05-12T00:00:00+00:00');",
            (json.dumps({
                "document_id": doc_id,
                "passage_id": pid,
                "proposed_canonical_label_en": "another concept",
                "proposed_description": "",
            }),),
        )
        conn.commit()

    res = _runner.invoke(app, ["concepts", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)

    # Both lists exist.
    assert "confirmed" in payload and "proposed" in payload
    assert len(payload["confirmed"]) == 1
    assert len(payload["proposed"]) == 2  # two distinct labels

    # Uniform `name` key across both.
    for item in payload["confirmed"]:
        assert "name" in item and item["name"]
    for item in payload["proposed"]:
        assert "name" in item and item["name"]
        # Back-compat alias kept:
        assert item["label"] == item["name"]
        # Slug present and stable.
        assert item["slug"] and " " not in item["slug"]
        assert isinstance(item["count"], int)

    # The dual-mention proposal is grouped (count=2):
    counts_by_name = {p["name"]: p["count"] for p in payload["proposed"]}
    assert counts_by_name["intentional mood"] == 2
    assert counts_by_name["another concept"] == 1
