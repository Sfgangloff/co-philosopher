"""Markdown ingestion.

Notes captured by ``cophilo dialog`` and any hand-written ``.md`` files in
the corpus are already close to the normalized form, so this ingester is
thin: parse optional YAML frontmatter, take the title from frontmatter / the
first ``# `` heading / the filename, and pass the body through the shared
whitespace normalizer. Remaining frontmatter keys are preserved as metadata.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter

from cophilo.ingest.normalize import NormalizedDoc, collapse_whitespace

# Keys that ``write_normalized`` sets itself — don't echo them back as
# document metadata or they would collide in the regenerated frontmatter.
_RESERVED_KEYS = {"title", "source", "source_format", "language"}


def ingest_md(source_path: Path) -> NormalizedDoc:
    post = frontmatter.loads(source_path.read_text(encoding="utf-8", errors="replace"))
    body = collapse_whitespace(post.content)

    title = (post.get("title") or "").strip() or None
    if title is None:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                title = stripped.lstrip("#").strip()
                break
        title = title or source_path.stem

    metadata = {
        k: v
        for k, v in post.metadata.items()
        if k not in _RESERVED_KEYS and v is not None
    }

    return NormalizedDoc(
        title=title,
        body=body,
        source_path=source_path,
        source_format="md",
        metadata=metadata,
    )
