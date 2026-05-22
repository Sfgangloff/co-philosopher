"""Thin sqlite3 helpers. Intentionally not an ORM — direct SQL is clearer for
this project's small, well-defined schema."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cophilo.config import Config

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def transaction(cfg: Config) -> Iterator[sqlite3.Connection]:
    conn = connect(cfg)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(cfg: Config) -> None:
    """Apply schema.sql idempotently and stamp the schema version."""
    sql = _SCHEMA_PATH.read_text()
    with transaction(cfg) as conn:
        conn.executescript(sql)
        cur = conn.execute("SELECT MAX(version) AS v FROM schema_version;")
        current = cur.fetchone()["v"]
        if current is None or current < SCHEMA_VERSION:
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?);",
                (SCHEMA_VERSION, _utcnow()),
            )


# --- Documents -------------------------------------------------------------


def insert_document(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str | None,
    source_path: str,
    normalized_path: str | None,
    language: str | None,
    metadata: dict[str, Any] | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO documents
          (kind, title, source_path, normalized_path, language, status, ingested_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, 'ingested', ?, ?)
        """,
        (
            kind,
            title,
            source_path,
            normalized_path,
            language,
            _utcnow(),
            json.dumps(metadata) if metadata else None,
        ),
    )
    return int(cur.lastrowid)


def find_document_by_source(conn: sqlite3.Connection, source_path: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM documents WHERE source_path = ?;", (source_path,))
    return cur.fetchone()


def list_documents(conn: sqlite3.Connection, kind: str | None = None) -> list[sqlite3.Row]:
    if kind is None:
        cur = conn.execute("SELECT * FROM documents ORDER BY ingested_at DESC;")
    else:
        cur = conn.execute(
            "SELECT * FROM documents WHERE kind = ? ORDER BY ingested_at DESC;",
            (kind,),
        )
    return cur.fetchall()


def set_document_status(conn: sqlite3.Connection, document_id: int, status: str) -> None:
    conn.execute("UPDATE documents SET status = ? WHERE id = ?;", (status, document_id))


def get_document(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE id = ?;", (document_id,)
    ).fetchone()


def update_document_source(
    conn: sqlite3.Connection, document_id: int, source_path: str
) -> None:
    """Repoint a document's source path (used when notes are moved into a
    draft folder). ``source_path`` is UNIQUE, so the caller must ensure the
    new path is free."""
    conn.execute(
        "UPDATE documents SET source_path = ? WHERE id = ?;",
        (source_path, document_id),
    )


# --- Concepts & questions (the extracted graph, for the CLI) ---------------


def list_concepts(
    conn: sqlite3.Connection,
    *,
    document_id: int | None = None,
    status: str = "confirmed",
) -> list[sqlite3.Row]:
    """Confirmed concepts with their mention counts, most-cited first.

    With ``document_id``, restrict to concepts mentioned in that document.
    """
    if document_id is None:
        return conn.execute(
            """
            SELECT c.id, c.slug, c.canonical_label_en, c.canonical_label_fr,
                   c.kind, c.description, COUNT(cm.id) AS mentions
            FROM concepts c
            LEFT JOIN concept_mentions cm ON cm.concept_id = c.id
            WHERE c.status = ?
            GROUP BY c.id
            ORDER BY mentions DESC, c.slug;
            """,
            (status,),
        ).fetchall()
    return conn.execute(
        """
        SELECT c.id, c.slug, c.canonical_label_en, c.canonical_label_fr,
               c.kind, c.description, COUNT(cm.id) AS mentions
        FROM concepts c
        JOIN concept_mentions cm ON cm.concept_id = c.id
        JOIN passages p ON p.id = cm.passage_id
        WHERE c.status = ? AND p.document_id = ?
        GROUP BY c.id
        ORDER BY mentions DESC, c.slug;
        """,
        (status, document_id),
    ).fetchall()


def list_concept_proposals(
    conn: sqlite3.Connection, *, document_id: int | None = None
) -> list[dict[str, Any]]:
    """Pending new-concept proposals from the review queue, grouped by label.

    Fresh extraction confirms nothing on its own — it *queues* proposals — so
    this is where the payoff of ``extract`` actually shows up.
    """
    rows = conn.execute(
        "SELECT payload_json FROM review_queue "
        "WHERE kind = 'new_concept' AND status = 'pending' ORDER BY id;"
    ).fetchall()
    # Lazy import: slugify is already a project-wide dependency (notes/draft),
    # but we keep the import local so this module stays small.
    from slugify import slugify

    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        payload = json.loads(r["payload_json"])
        if document_id is not None and payload.get("document_id") != document_id:
            continue
        label = (
            payload.get("proposed_canonical_label_en")
            or payload.get("proposed_canonical_label_fr")
            or "(unlabelled)"
        )
        slot = agg.setdefault(
            label,
            {
                # `name` matches the primary identifier used by confirmed
                # concepts (`canonical_label_en`) so JSON consumers (the
                # philosopher's "--json for tooling / Claude Code") can rely
                # on one key across both kinds. `label` is kept as an alias
                # for back-compat with any existing scripts.
                "name": label,
                "label": label,
                "slug": slugify(label, max_length=80) or "concept",
                "description": payload.get("proposed_description") or "",
                "count": 0,
            },
        )
        slot["count"] += 1
    return sorted(agg.values(), key=lambda d: (-d["count"], d["name"]))


def list_questions(
    conn: sqlite3.Connection, *, document_id: int | None = None
) -> list[sqlite3.Row]:
    """Open/answered questions with mention counts, most-cited first."""
    if document_id is None:
        return conn.execute(
            """
            SELECT q.id, q.label, q.description, q.status,
                   COUNT(qm.passage_id) AS mentions
            FROM questions q
            LEFT JOIN question_mentions qm ON qm.question_id = q.id
            GROUP BY q.id
            ORDER BY mentions DESC, q.id;
            """
        ).fetchall()
    return conn.execute(
        """
        SELECT q.id, q.label, q.description, q.status,
               COUNT(qm.passage_id) AS mentions
        FROM questions q
        JOIN question_mentions qm ON qm.question_id = q.id
        JOIN passages p ON p.id = qm.passage_id
        WHERE p.document_id = ?
        GROUP BY q.id
        ORDER BY mentions DESC, q.id;
        """,
        (document_id,),
    ).fetchall()


# --- Cross-document concept graph (§2.3) -----------------------------------


def list_concepts_with_spread(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Confirmed concepts ordered by cross-document spread (then mentions).

    REPORT.md §2.3 — the value of `extract` is the *graph between concepts
    across notes*, not a flat per-doc list. ``doc_count`` is what surfaces
    that: a concept mentioned twenty times in one note matters less to the
    project than one mentioned twice in five distinct notes."""
    return conn.execute(
        """
        SELECT c.id, c.slug, c.canonical_label_en, c.canonical_label_fr,
               c.kind, c.description,
               COUNT(cm.id) AS mentions,
               COUNT(DISTINCT p.document_id) AS doc_count
        FROM concepts c
        LEFT JOIN concept_mentions cm ON cm.concept_id = c.id
        LEFT JOIN passages p ON p.id = cm.passage_id
        WHERE c.status = 'confirmed'
        GROUP BY c.id
        ORDER BY doc_count DESC, mentions DESC, c.slug;
        """
    ).fetchall()


def concept_co_occurrences(
    conn: sqlite3.Connection, *, min_shared: int = 2, limit: int = 20
) -> list[sqlite3.Row]:
    """Pairs of confirmed concepts that share at least ``min_shared``
    documents. Co-occurrence at *document* (not passage) level — the
    project-relevant notion of "two notes converging on the same pair"."""
    return conn.execute(
        """
        WITH concept_doc AS (
            SELECT DISTINCT cm.concept_id, p.document_id
            FROM concept_mentions cm
            JOIN passages p ON p.id = cm.passage_id
            JOIN concepts c ON c.id = cm.concept_id
            WHERE c.status = 'confirmed'
        ),
        pairs AS (
            SELECT a.concept_id AS a_id, b.concept_id AS b_id,
                   COUNT(*) AS shared
            FROM concept_doc a
            JOIN concept_doc b
              ON b.document_id = a.document_id AND a.concept_id < b.concept_id
            GROUP BY a.concept_id, b.concept_id
            HAVING COUNT(*) >= ?
        )
        SELECT p.a_id, p.b_id, p.shared,
               ca.slug AS a_slug, ca.canonical_label_en AS a_en,
               ca.canonical_label_fr AS a_fr,
               cb.slug AS b_slug, cb.canonical_label_en AS b_en,
               cb.canonical_label_fr AS b_fr
        FROM pairs p
        JOIN concepts ca ON ca.id = p.a_id
        JOIN concepts cb ON cb.id = p.b_id
        ORDER BY p.shared DESC, ca.slug, cb.slug
        LIMIT ?;
        """,
        (min_shared, limit),
    ).fetchall()


def find_concept(conn: sqlite3.Connection, query: str) -> sqlite3.Row | None:
    """Best-effort lookup by slug, English label, or French label.

    Tries exact-slug, then exact-label (either language), then substring on
    any of them. Returns the first match (or ``None``)."""
    q = query.strip().lower()
    if not q:
        return None
    # Exact slug — the canonical id.
    row = conn.execute(
        "SELECT * FROM concepts WHERE LOWER(slug) = ? AND status = 'confirmed' LIMIT 1;",
        (q,),
    ).fetchone()
    if row is not None:
        return row
    # Exact canonical label, either language.
    row = conn.execute(
        """
        SELECT * FROM concepts
        WHERE (LOWER(canonical_label_en) = ? OR LOWER(canonical_label_fr) = ?)
          AND status = 'confirmed'
        LIMIT 1;
        """,
        (q, q),
    ).fetchone()
    if row is not None:
        return row
    # Substring fallback.
    like = f"%{q}%"
    return conn.execute(
        """
        SELECT * FROM concepts
        WHERE (LOWER(slug) LIKE ?
            OR LOWER(canonical_label_en) LIKE ?
            OR LOWER(canonical_label_fr) LIKE ?)
          AND status = 'confirmed'
        ORDER BY slug
        LIMIT 1;
        """,
        (like, like, like),
    ).fetchone()


def concept_docs(conn: sqlite3.Connection, concept_id: int) -> list[sqlite3.Row]:
    """Documents that mention a concept, with the per-doc mention count."""
    return conn.execute(
        """
        SELECT d.id, d.title, d.source_path, d.kind,
               COUNT(cm.id) AS mentions
        FROM documents d
        JOIN passages p ON p.document_id = d.id
        JOIN concept_mentions cm ON cm.passage_id = p.id
        WHERE cm.concept_id = ?
        GROUP BY d.id
        ORDER BY mentions DESC, d.title;
        """,
        (concept_id,),
    ).fetchall()


def concept_neighbors(
    conn: sqlite3.Connection, concept_id: int, *, limit: int = 10
) -> list[sqlite3.Row]:
    """Confirmed concepts that share documents with this one, ranked by
    how many documents they co-appear in."""
    return conn.execute(
        """
        WITH our_docs AS (
            SELECT DISTINCT p.document_id
            FROM concept_mentions cm
            JOIN passages p ON p.id = cm.passage_id
            WHERE cm.concept_id = ?
        )
        SELECT c.id, c.slug, c.canonical_label_en, c.canonical_label_fr,
               COUNT(DISTINCT p.document_id) AS shared_docs
        FROM concepts c
        JOIN concept_mentions cm ON cm.concept_id = c.id
        JOIN passages p ON p.id = cm.passage_id
        WHERE p.document_id IN (SELECT document_id FROM our_docs)
          AND c.id != ?
          AND c.status = 'confirmed'
        GROUP BY c.id
        ORDER BY shared_docs DESC, c.slug
        LIMIT ?;
        """,
        (concept_id, concept_id, limit),
    ).fetchall()


def concept_questions(
    conn: sqlite3.Connection, concept_id: int, *, limit: int = 10
) -> list[sqlite3.Row]:
    """Questions raised in the same documents where a concept appears.

    Surfaces the "which questions does this concept push you toward?" view
    — the cross-doc dual of `cophilo questions --doc <id>`."""
    return conn.execute(
        """
        WITH our_docs AS (
            SELECT DISTINCT p.document_id
            FROM concept_mentions cm
            JOIN passages p ON p.id = cm.passage_id
            WHERE cm.concept_id = ?
        )
        SELECT q.id, q.label, q.status,
               COUNT(DISTINCT p.document_id) AS docs
        FROM questions q
        JOIN question_mentions qm ON qm.question_id = q.id
        JOIN passages p ON p.id = qm.passage_id
        WHERE p.document_id IN (SELECT document_id FROM our_docs)
        GROUP BY q.id
        ORDER BY docs DESC, q.label
        LIMIT ?;
        """,
        (concept_id, limit),
    ).fetchall()


# --- Bibliography ----------------------------------------------------------


def upsert_bibliography(
    conn: sqlite3.Connection,
    *,
    source: str,
    external_id: str | None,
    title: str,
    authors: str | None,
    journal: str | None,
    year: int | None,
    abstract: str | None,
    doi: str | None = None,
    quality_score: float | None = None,
) -> int:
    """Insert or refresh a bibliography row, keyed on (source, external_id).

    Idempotent: re-fetching the same record updates its fields and
    ``fetched_at`` rather than creating duplicates.
    """
    conn.execute(
        """
        INSERT INTO bibliography
            (source, external_id, title, authors, journal, year, abstract,
             doi, quality_score, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, external_id) DO UPDATE SET
            title = excluded.title,
            authors = excluded.authors,
            journal = excluded.journal,
            year = excluded.year,
            abstract = excluded.abstract,
            doi = excluded.doi,
            quality_score = COALESCE(excluded.quality_score, bibliography.quality_score),
            fetched_at = excluded.fetched_at;
        """,
        (
            source,
            external_id,
            title,
            authors,
            journal,
            year,
            abstract,
            doi,
            quality_score,
            _utcnow(),
        ),
    )
    row = conn.execute(
        "SELECT id FROM bibliography WHERE source = ? AND external_id IS ?;",
        (source, external_id),
    ).fetchone()
    return int(row["id"])
