"""DOCX ingestion via pandoc."""

from __future__ import annotations

from pathlib import Path

import pypandoc

from cophilo.ingest.normalize import NormalizedDoc, collapse_whitespace


def ingest_docx(source_path: Path) -> NormalizedDoc:
    body = pypandoc.convert_file(
        str(source_path),
        to="markdown_strict+pipe_tables+yaml_metadata_block",
        format="docx",
        extra_args=["--wrap=none"],
    )
    body = collapse_whitespace(body)

    title = _first_heading(body) or source_path.stem
    return NormalizedDoc(
        title=title,
        body=body,
        source_path=source_path,
        source_format="docx",
        metadata={},
    )


def _first_heading(md: str) -> str | None:
    for line in md.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
        if line and not line.startswith(("---", "<!--")):
            # First non-blank line; use as title if no heading exists.
            if len(line) <= 200:
                return line
            return None
    return None
