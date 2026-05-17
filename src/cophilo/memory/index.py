"""High-level memory operations: build the store, keep it fresh, search it."""

from __future__ import annotations

from cophilo.config import Config
from cophilo.memory import store
from cophilo.memory.embed import EmbedderLike, default_embedder
from cophilo.memory.journals import load_journals, source_hash


def build(cfg: Config, embedder: EmbedderLike | None = None) -> int:
    """(Re)build the journals vector store from ``data/journals.yaml``."""
    emb = embedder or default_embedder(cfg)
    records = load_journals(cfg)
    conn = store.connect(cfg.memory_db_path)
    try:
        return store.rebuild(conn, records, emb)
    finally:
        conn.close()


def _ensure_fresh(cfg: Config, emb: EmbedderLike) -> None:
    """Rebuild only if the store is missing or out of date.

    The signature pins the embedder model, vector dim, and a hash of the
    whole catalog, so an edited ``journals.yaml`` or a changed model
    transparently triggers a rebuild.
    """
    records = load_journals(cfg)
    want = f"{emb.name}|{emb.dim}|{source_hash(records)}"
    conn = store.connect(cfg.memory_db_path)
    try:
        if store.current_signature(conn) == want:
            return
    finally:
        conn.close()
    build(cfg, embedder=emb)


def search(
    cfg: Config,
    query: str,
    *,
    limit: int = 8,
    open_access_only: bool = False,
    embedder: EmbedderLike | None = None,
) -> list[dict]:
    query = (query or "").strip()
    if not query:
        raise ValueError("query must be non-empty")
    emb = embedder or default_embedder(cfg)
    _ensure_fresh(cfg, emb)
    qv = emb.encode([query])[0].tolist()
    conn = store.connect(cfg.memory_db_path)
    try:
        return store.search(
            conn, qv, limit=limit, open_access_only=open_access_only
        )
    finally:
        conn.close()
