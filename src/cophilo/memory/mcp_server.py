"""MCP server exposing the journals catalog as semantic memory.

Run by Claude Code via the project's ``.mcp.json`` (stdio transport).
The vector store is built lazily on first call and self-heals when
``data/journals.yaml`` or the embedding model changes.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from cophilo.config import get_config
from cophilo.memory import index
from cophilo.memory.store import connect, get_by_name

mcp = FastMCP("cophilo-memory")


@mcp.tool()
def search_journals(
    query: str, limit: int = 8, open_access_only: bool = False
) -> list[dict]:
    """Semantically search the curated philosophy-journals catalog.

    Use this to find which journals fit a paper's topic/subfield. Returns
    journals ranked by relevance with scope, typical article length, word
    cap, open-access status, and URL.

    query: a topic, subfield, or abstract (e.g. "modal accounts of free
        will and the ability to do otherwise").
    limit: max journals to return.
    open_access_only: restrict to open-access journals.
    """
    cfg = get_config()
    return index.search(
        cfg, query, limit=limit, open_access_only=open_access_only
    )


@mcp.tool()
def get_journal(name: str) -> dict | None:
    """Look up one journal by (approximate) name; null if not found."""
    cfg = get_config()
    conn = connect(cfg.memory_db_path)
    try:
        return get_by_name(conn, name)
    finally:
        conn.close()


def main() -> None:
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
