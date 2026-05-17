"""Local semantic memory: a sqlite-vec store over the journals catalog,
exposed to Claude Code via an MCP server.

The store is a *derived* artifact: ``data/journals.yaml`` is the
git-tracked source of truth, ``data/db/memory.sqlite`` is rebuilt from it
(``cophilo memory index``) and is gitignored like the other databases.
"""

from cophilo.memory.index import build, search

__all__ = ["build", "search"]
