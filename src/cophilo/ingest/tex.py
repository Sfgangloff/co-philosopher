"""LaTeX ingestion via pandoc.

A pre-pass scans the source for ``\\concept{...}`` macros. Each match is
recorded in metadata as a hint that the user already treats the term as a
concept, which the extraction pass uses to seed candidates.
"""

from __future__ import annotations

import re
from pathlib import Path

import pypandoc

from cophilo.ingest.normalize import NormalizedDoc, collapse_whitespace

_CONCEPT_MACRO_RE = re.compile(r"\\concept\{([^{}]+)\}")
_TITLE_RE = re.compile(r"\\title\{([^{}]+)\}")


def _harvest_concept_hints(tex: str) -> list[str]:
    seen = []
    seen_set: set[str] = set()
    for m in _CONCEPT_MACRO_RE.finditer(tex):
        term = m.group(1).strip()
        key = term.casefold()
        if term and key not in seen_set:
            seen.append(term)
            seen_set.add(key)
    return seen


def ingest_tex(source_path: Path) -> NormalizedDoc:
    raw = source_path.read_text(encoding="utf-8", errors="replace")
    concept_hints = _harvest_concept_hints(raw)

    title_match = _TITLE_RE.search(raw)
    title = title_match.group(1).strip() if title_match else None

    body = pypandoc.convert_file(
        str(source_path),
        to="markdown_strict+pipe_tables+yaml_metadata_block",
        format="latex",
        extra_args=["--wrap=none"],
    )
    body = collapse_whitespace(body)

    if title is None:
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("#"):
                title = line.lstrip("#").strip()
                break
        title = title or source_path.stem

    metadata: dict = {}
    if concept_hints:
        metadata["concept_hints"] = concept_hints

    return NormalizedDoc(
        title=title,
        body=body,
        source_path=source_path,
        source_format="tex",
        metadata=metadata,
    )
