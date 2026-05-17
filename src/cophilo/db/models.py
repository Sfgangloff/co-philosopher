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
