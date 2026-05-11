"""PDF ingestion via PyMuPDF.

Extracts text page by page, preserves page boundaries as HTML comments so
later passes can still resolve "this passage is on page N", and applies a
light heuristic to demote running headers/footers that repeat across pages.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

from cophilo.ingest.normalize import NormalizedDoc, collapse_whitespace


def _extract_title(doc: fitz.Document, source_path: Path) -> str | None:
    meta_title = (doc.metadata or {}).get("title", "").strip() if doc.metadata else ""
    if meta_title:
        return meta_title
    if doc.page_count == 0:
        return source_path.stem
    # First non-empty line of page 1 is a reasonable title fallback.
    page1 = doc[0].get_text("text").strip().splitlines()
    for line in page1:
        line = line.strip()
        if len(line) > 3:
            return line
    return source_path.stem


def _drop_repeating_lines(pages: list[str], min_repeats: int = 3) -> list[str]:
    """Strip lines that recur on many pages (typical for headers/footers)."""
    if len(pages) < min_repeats:
        return pages
    line_counts: Counter[str] = Counter()
    for page in pages:
        for line in {ln.strip() for ln in page.splitlines() if ln.strip()}:
            line_counts[line] += 1
    threshold = max(min_repeats, len(pages) // 3)
    repeating = {ln for ln, n in line_counts.items() if n >= threshold and len(ln) < 120}

    cleaned = []
    for page in pages:
        kept = [ln for ln in page.splitlines() if ln.strip() not in repeating]
        cleaned.append("\n".join(kept))
    return cleaned


def ingest_pdf(source_path: Path) -> NormalizedDoc:
    doc = fitz.open(source_path)
    try:
        title = _extract_title(doc, source_path)
        pages = [page.get_text("text") for page in doc]
        cleaned = _drop_repeating_lines(pages)

        chunks = []
        for i, page_text in enumerate(cleaned, start=1):
            chunks.append(f"<!-- page {i} -->")
            chunks.append(page_text.strip())
        body = collapse_whitespace("\n\n".join(chunks))

        metadata = {}
        m = doc.metadata or {}
        for k in ("author", "subject", "creator"):
            v = (m.get(k) or "").strip()
            if v:
                metadata[k] = v
        metadata["page_count"] = doc.page_count

        return NormalizedDoc(
            title=title,
            body=body,
            source_path=source_path,
            source_format="pdf",
            metadata=metadata,
        )
    finally:
        doc.close()
