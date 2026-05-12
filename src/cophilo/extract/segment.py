"""Split a normalized markdown document into passages.

A passage is one paragraph (separated by blank lines), with a section_path
derived from the most recent ATX headings (`#`, `##`, `###`).

Char offsets are computed against the body text only — frontmatter is
stripped first. The body, not the source file, is what subsequent passes
operate on.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from cophilo.config import Config
from cophilo.db import models as db

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_PAGE_MARKER_RE = re.compile(r"^<!--\s*page\s+\d+\s*-->\s*$")
_HTML_COMMENT_RE = re.compile(r"^<!--.*-->\s*$")


@dataclass
class Passage:
    ord: int
    char_start: int
    char_end: int
    section_path: str
    text: str


def load_body(normalized_path: Path) -> str:
    """Return the body text (markdown) of a normalized document, dropping
    the YAML frontmatter."""
    post = frontmatter.loads(normalized_path.read_text(encoding="utf-8"))
    return post.content


def segment(body: str) -> list[Passage]:
    """Split the body into passages.

    A blank-line separated group of non-heading, non-page-marker lines is one
    passage. Headings update the rolling section path but are not themselves
    passages.
    """
    section_stack: list[str] = []  # one entry per heading level present
    passages: list[Passage] = []
    ord_counter = 0

    pos = 0
    body_len = len(body)
    current_lines: list[tuple[int, int, str]] = []  # (start, end, text)

    def flush() -> None:
        nonlocal ord_counter
        if not current_lines:
            return
        start = current_lines[0][0]
        end = current_lines[-1][1]
        text = body[start:end].strip()
        if text:
            ord_counter += 1
            passages.append(
                Passage(
                    ord=ord_counter,
                    char_start=start,
                    char_end=end,
                    section_path="/".join(section_stack),
                    text=text,
                )
            )
        current_lines.clear()

    while pos < body_len:
        nl = body.find("\n", pos)
        line_end = body_len if nl == -1 else nl
        line = body[pos:line_end]
        line_start = pos
        # Advance past this line (and the newline if present).
        pos = body_len if nl == -1 else nl + 1

        stripped = line.strip()

        if not stripped:
            flush()
            continue

        # Headings update section_stack and never start passages.
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            # Trim or pad the stack to `level`, then set the title at this level.
            section_stack[:] = section_stack[: level - 1]
            while len(section_stack) < level - 1:
                section_stack.append("")
            section_stack.append(title)
            continue

        # Page markers and other comment-only lines: skip but don't break flow.
        if _PAGE_MARKER_RE.match(line) or _HTML_COMMENT_RE.match(line):
            continue

        current_lines.append((line_start, line_end, line))

    flush()
    return passages


def persist_passages(conn: sqlite3.Connection, document_id: int, passages: list[Passage]) -> int:
    """Replace any existing passages for the document with the provided list.

    Returns the number of inserted passages. Cascades wipe dependent
    concept_mentions, claims, etc. — segmentation should be a deliberate
    re-do.
    """
    conn.execute("DELETE FROM passages WHERE document_id = ?;", (document_id,))
    rows = [
        (document_id, p.ord, p.char_start, p.char_end, p.section_path, p.text)
        for p in passages
    ]
    conn.executemany(
        """
        INSERT INTO passages (document_id, ord, char_start, char_end, section_path, text)
        VALUES (?, ?, ?, ?, ?, ?);
        """,
        rows,
    )
    return len(rows)


def segment_document(cfg: Config, document_id: int) -> int:
    """Load the normalized doc, segment it, persist passages.

    Returns the number of passages persisted.
    """
    with db.transaction(cfg) as conn:
        row = conn.execute(
            "SELECT normalized_path FROM documents WHERE id = ?;", (document_id,)
        ).fetchone()
        if row is None or row["normalized_path"] is None:
            raise ValueError(f"document {document_id} has no normalized_path")
        body = load_body(Path(row["normalized_path"]))
        passages = segment(body)
        return persist_passages(conn, document_id, passages)
