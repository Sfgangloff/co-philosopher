"""Extraction pass orchestrator.

For each document, run the concept and question passes against Claude,
validate the structured response, then persist:

  - confirmed-concept mentions go into ``concept_mentions``
  - new-concept proposals go into ``review_queue`` (kind='new_concept')
  - question mentions go into ``question_mentions`` (creating questions
    on first sight, attached to subsequent passages on later mentions)
  - external-author attributions go into ``external_authors`` and
    ``concept_external_authors``

Passages must already be persisted via ``extract.segment``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from slugify import slugify

from cophilo.config import Config
from cophilo.db import models as db
from cophilo.extract.claude import LLMClient, load_prompt_template, make_client
from cophilo.extract.schemas import (
    ConceptMention,
    ConceptPassResponse,
    QuestionMention,
    QuestionPassResponse,
)
from cophilo.extract.segment import segment_document


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PassStats:
    document_id: int
    confirmed_mentions: int = 0
    new_concept_proposals: int = 0
    question_mentions: int = 0
    new_questions: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


# --- prompt assembly -----------------------------------------------------


def _format_taxonomy(conn: sqlite3.Connection) -> str:
    """Render the confirmed taxonomy as a stable, deterministic string.

    Sorted by slug so byte order is stable across runs (load-bearing for
    prompt-cache hits).
    """
    rows = conn.execute(
        """
        SELECT slug, canonical_label_en, canonical_label_fr, description, kind
        FROM concepts
        WHERE status = 'confirmed'
        ORDER BY slug;
        """
    ).fetchall()
    if not rows:
        return "(no concepts confirmed yet)"
    parts = []
    for row in rows:
        parts.append(
            f"- slug: {row['slug']}\n"
            f"  EN: {row['canonical_label_en'] or ''}\n"
            f"  FR: {row['canonical_label_fr'] or ''}\n"
            f"  kind: {row['kind']}\n"
            f"  description: {(row['description'] or '').strip()}"
        )
    return "\n".join(parts)


def _format_passages(passages: list[sqlite3.Row]) -> str:
    chunks = []
    for row in passages:
        section = row["section_path"] or ""
        header = f"[passage {row['ord']}]"
        if section:
            header += f" — section: {section}"
        chunks.append(f"{header}\n{row['text']}")
    return "\n\n".join(chunks)


def _build_system_prompt(template: str, taxonomy: str, title: str, language: str, passages: str) -> str:
    return template.format(taxonomy=taxonomy, title=title, language=language, passages=passages)


# --- persistence helpers -------------------------------------------------


def _resolve_span(passage_text: str, span_quote: str) -> tuple[int, int] | None:
    """Find an exact match for ``span_quote`` inside ``passage_text``.

    Returns (start, end) byte offsets *within the passage*, or None if no
    exact match. Span back-resolution is left exact in M2; fuzzy matching
    arrives in M4 alongside the annotated rendering.
    """
    if not span_quote:
        return None
    idx = passage_text.find(span_quote)
    if idx < 0:
        return None
    return (idx, idx + len(span_quote))


def _passage_id_by_ord(conn: sqlite3.Connection, document_id: int) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM passages WHERE document_id = ? ORDER BY ord;",
        (document_id,),
    ).fetchall()
    return {row["ord"]: row for row in rows}


def _persist_concept_mention(
    conn: sqlite3.Connection, *, concept_id: int, passage: sqlite3.Row, mention: ConceptMention
) -> None:
    span = _resolve_span(passage["text"], mention.span_quote)
    span_start, span_end = (span if span else (None, None))
    conn.execute(
        """
        INSERT INTO concept_mentions
            (concept_id, passage_id, span_start, span_end, role, confidence)
        VALUES (?, ?, ?, ?, ?, ?);
        """,
        (concept_id, passage["id"], span_start, span_end, mention.role, mention.confidence),
    )
    for author in mention.attributed_authors:
        author = author.strip()
        if not author:
            continue
        normalized = author.casefold()
        conn.execute(
            "INSERT OR IGNORE INTO external_authors (name, normalized_name) VALUES (?, ?);",
            (author, normalized),
        )
        author_id = conn.execute(
            "SELECT id FROM external_authors WHERE normalized_name = ?;", (normalized,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO concept_external_authors (concept_id, author_id) VALUES (?, ?);",
            (concept_id, author_id),
        )


def _enqueue_new_concept(
    conn: sqlite3.Connection, *, document_id: int, passage: sqlite3.Row, mention: ConceptMention
) -> None:
    payload = {
        "document_id": document_id,
        "passage_id": passage["id"],
        "passage_ord": passage["ord"],
        "passage_text": passage["text"],
        "span_quote": mention.span_quote,
        "proposed_canonical_label_en": mention.proposed_canonical_label_en,
        "proposed_canonical_label_fr": mention.proposed_canonical_label_fr,
        "proposed_description": mention.proposed_description,
        "role": mention.role,
        "confidence": mention.confidence,
        "attributed_authors": mention.attributed_authors,
    }
    conn.execute(
        """
        INSERT INTO review_queue (kind, payload_json, created_at)
        VALUES ('new_concept', ?, ?);
        """,
        (json.dumps(payload, ensure_ascii=False), _utcnow()),
    )


def _find_concept_id_by_slug(conn: sqlite3.Connection, slug: str) -> int | None:
    row = conn.execute(
        "SELECT id, status, merged_into FROM concepts WHERE slug = ?;",
        (slug,),
    ).fetchone()
    if row is None:
        return None
    if row["status"] == "merged" and row["merged_into"] is not None:
        return int(row["merged_into"])
    return int(row["id"])


# --- question persistence ------------------------------------------------


def _question_label_slug(label: str) -> str:
    return slugify(label, max_length=80) or "question"


def _find_or_create_question(
    conn: sqlite3.Connection, *, q: QuestionMention, passage: sqlite3.Row
) -> tuple[int, bool]:
    """Match a question by label slug; create on miss. Returns (id, created)."""
    slug = _question_label_slug(q.label)
    # Use a like-based lookup against label slug for approximate dedup.
    existing = conn.execute(
        "SELECT id FROM questions WHERE label = ? OR label = ?;",
        (q.label, slug),
    ).fetchone()
    if existing is not None:
        return int(existing["id"]), False
    cur = conn.execute(
        """
        INSERT INTO questions (label, description, status, first_raised_passage_id)
        VALUES (?, ?, 'open', ?);
        """,
        (q.label, q.description, passage["id"]),
    )
    return int(cur.lastrowid), True


# --- pass runners --------------------------------------------------------


def _run_concept_pass(
    conn: sqlite3.Connection,
    *,
    cfg: Config,
    client: LLMClient,
    document_id: int,
    title: str,
    language: str,
    passages_by_ord: dict[int, sqlite3.Row],
    stats: PassStats,
) -> None:
    template = load_prompt_template(language, "concepts")
    taxonomy = _format_taxonomy(conn)
    rendered_passages = _format_passages(list(passages_by_ord.values()))
    system = _build_system_prompt(template, taxonomy, title, language, rendered_passages)

    user = (
        "Extract concept mentions from the document above. "
        "Return JSON conforming to ConceptPassResponse."
    )

    result = client.call(
        model=cfg.claude_model_routine,
        system=system,
        user=user,
        response_model=ConceptPassResponse,
        max_tokens=8000,
    )
    stats.cache_read_tokens += result.cache_read_tokens
    stats.cache_write_tokens += result.cache_write_tokens
    stats.input_tokens += result.input_tokens
    stats.output_tokens += result.output_tokens

    parsed: ConceptPassResponse = result.parsed  # type: ignore[assignment]
    for mention in parsed.mentions:
        if mention.confidence < 0.4:
            continue
        passage = passages_by_ord.get(mention.passage_ord)
        if passage is None:
            continue

        if not mention.is_new and mention.slug:
            cid = _find_concept_id_by_slug(conn, mention.slug)
            if cid is None:
                # Slug claimed but not found — treat as a new-concept proposal
                # so a human can decide whether the slug was a hallucination.
                _enqueue_new_concept(conn, document_id=document_id, passage=passage, mention=mention)
                stats.new_concept_proposals += 1
                continue
            _persist_concept_mention(conn, concept_id=cid, passage=passage, mention=mention)
            stats.confirmed_mentions += 1
        else:
            _enqueue_new_concept(conn, document_id=document_id, passage=passage, mention=mention)
            stats.new_concept_proposals += 1


def _run_question_pass(
    conn: sqlite3.Connection,
    *,
    cfg: Config,
    client: LLMClient,
    document_id: int,
    title: str,
    language: str,
    passages_by_ord: dict[int, sqlite3.Row],
    stats: PassStats,
) -> None:
    template = load_prompt_template(language, "questions")
    rendered_passages = _format_passages(list(passages_by_ord.values()))
    # The question prompt has no taxonomy field; passing an empty string is fine.
    system = template.format(taxonomy="", title=title, language=language, passages=rendered_passages)
    user = (
        "Extract open questions from the document above. "
        "Return JSON conforming to QuestionPassResponse."
    )
    result = client.call(
        model=cfg.claude_model_routine,
        system=system,
        user=user,
        response_model=QuestionPassResponse,
        max_tokens=4000,
    )
    stats.cache_read_tokens += result.cache_read_tokens
    stats.cache_write_tokens += result.cache_write_tokens
    stats.input_tokens += result.input_tokens
    stats.output_tokens += result.output_tokens

    parsed: QuestionPassResponse = result.parsed  # type: ignore[assignment]
    for q in parsed.questions:
        if q.confidence < 0.4:
            continue
        passage = passages_by_ord.get(q.passage_ord)
        if passage is None:
            continue
        qid, created = _find_or_create_question(conn, q=q, passage=passage)
        if created:
            stats.new_questions += 1
        conn.execute(
            "INSERT OR IGNORE INTO question_mentions (question_id, passage_id, role) VALUES (?, ?, ?);",
            (qid, passage["id"], q.role),
        )
        stats.question_mentions += 1


# --- public API ----------------------------------------------------------


def extract_document(
    cfg: Config,
    document_id: int,
    *,
    client: LLMClient | None = None,
    passes: tuple[str, ...] = ("concepts", "questions"),
) -> PassStats:
    """Run extraction passes for one document.

    Re-segments the document first (idempotent — drops and re-inserts
    passages). Returns aggregate stats. ``client`` is injectable so tests
    can swap in a fake.
    """
    segment_document(cfg, document_id)
    if client is None:
        client = make_client(cfg)

    stats = PassStats(document_id=document_id)
    with db.transaction(cfg) as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?;", (document_id,)).fetchone()
        if doc is None:
            raise ValueError(f"document {document_id} not found")
        title = doc["title"] or ""
        language = doc["language"] or cfg.default_language
        passages_by_ord = _passage_id_by_ord(conn, document_id)
        if not passages_by_ord:
            return stats

        if "concepts" in passes:
            _run_concept_pass(
                conn,
                cfg=cfg,
                client=client,
                document_id=document_id,
                title=title,
                language=language,
                passages_by_ord=passages_by_ord,
                stats=stats,
            )
        if "questions" in passes:
            _run_question_pass(
                conn,
                cfg=cfg,
                client=client,
                document_id=document_id,
                title=title,
                language=language,
                passages_by_ord=passages_by_ord,
                stats=stats,
            )

        db.set_document_status(conn, document_id, "extracted")

    return stats
