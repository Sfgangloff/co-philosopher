"""sqlite-vec backed store for the journals catalog.

The store is fully rebuilt from ``data/journals.yaml`` on every index
(128 rows — cheap, and it cleanly handles edits/removals). A ``meta`` row
records the embedder + catalog fingerprint so a stale store self-heals.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from cophilo.memory.embed import EmbedderLike
from cophilo.memory.journals import Journal, source_hash


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _ensure_schema(conn: sqlite3.Connection, dim: int) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS journals (
            id             INTEGER PRIMARY KEY,
            slug           TEXT UNIQUE NOT NULL,
            name           TEXT NOT NULL,
            publisher      TEXT,
            scope          TEXT,
            typical_length TEXT,
            max_words      INTEGER,
            open_access    INTEGER NOT NULL DEFAULT 0,
            url            TEXT,
            issn           TEXT,
            content_hash   TEXT NOT NULL
        );
        """
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS journals_vec "
        f"USING vec0(embedding float[{dim}] distance_metric=cosine);"
    )


def _signature(model: str, dim: int, src_hash: str) -> str:
    return f"{model}|{dim}|{src_hash}"


def current_signature(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'signature';").fetchone()
    except sqlite3.OperationalError:
        return None
    return row["value"] if row else None


def rebuild(
    conn: sqlite3.Connection,
    records: list[Journal],
    embedder: EmbedderLike,
) -> int:
    vectors = embedder.encode([r.embedding_text for r in records])
    dim = int(vectors.shape[1])

    with conn:
        _ensure_schema(conn, dim)
        conn.execute("DELETE FROM journals;")
        conn.execute("DELETE FROM journals_vec;")
        for rec, vec in zip(records, vectors, strict=True):
            cur = conn.execute(
                """
                INSERT INTO journals
                  (slug, name, publisher, scope, typical_length,
                   max_words, open_access, url, issn, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.slug,
                    rec.name,
                    rec.publisher,
                    rec.scope,
                    rec.typical_length,
                    rec.max_words,
                    int(rec.open_access),
                    rec.url,
                    rec.issn,
                    rec.content_hash,
                ),
            )
            conn.execute(
                "INSERT INTO journals_vec (rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, sqlite_vec.serialize_float32(vec.tolist())),
            )
        sig = _signature(embedder.name, dim, source_hash(records))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('signature', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (sig,),
        )
    return len(records)


def _row_to_dict(row: sqlite3.Row, distance: float) -> dict:
    return {
        "name": row["name"],
        "publisher": row["publisher"],
        "scope": row["scope"],
        "typical_length": row["typical_length"],
        "max_words": row["max_words"],
        "open_access": bool(row["open_access"]),
        "url": row["url"],
        "issn": row["issn"],
        "score": round(1.0 - float(distance), 4),
    }


def search(
    conn: sqlite3.Connection,
    query_vec,
    *,
    limit: int = 8,
    open_access_only: bool = False,
) -> list[dict]:
    total = conn.execute("SELECT COUNT(*) AS n FROM journals;").fetchone()["n"]
    if not total:
        return []
    # Over-fetch then filter open-access in Python: the catalog is tiny and
    # vec0 metadata filtering varies across sqlite-vec versions.
    k = min(total, max(limit * 8, limit) if open_access_only else max(limit, 1))
    rows = conn.execute(
        """
        SELECT j.*, v.distance AS distance
        FROM journals_vec v
        JOIN journals j ON j.id = v.rowid
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
        """,
        (sqlite_vec.serialize_float32(list(query_vec)), k),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        if open_access_only and not r["open_access"]:
            continue
        out.append(_row_to_dict(r, r["distance"]))
        if len(out) >= limit:
            break
    return out


def get_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    from slugify import slugify

    slug = slugify(name)
    row = conn.execute("SELECT * FROM journals WHERE slug = ?;", (slug,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM journals WHERE name LIKE ? ORDER BY length(name) LIMIT 1;",
            (f"%{name}%",),
        ).fetchone()
    return _row_to_dict(row, 0.0) if row else None
