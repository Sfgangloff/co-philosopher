"""Thin sqlite3 helpers. Intentionally not an ORM — direct SQL is clearer for
this project's small, well-defined schema."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from cophilo.config import Config

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
