"""Format dispatcher: pick the right ingester for a file and orchestrate the
write to disk + DB insert.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from cophilo.config import Config
from cophilo.db import models as db
from cophilo.ingest.docx import ingest_docx
from cophilo.ingest.normalize import (
    NormalizedDoc,
    detect_format,
    detect_language,
    write_normalized,
)
from cophilo.ingest.pdf import ingest_pdf
from cophilo.ingest.tex import ingest_tex

INGESTERS = {
    "pdf": ingest_pdf,
    "docx": ingest_docx,
    "tex": ingest_tex,
}

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".tex"}


class UnsupportedFormatError(ValueError):
    pass


def ingest_file(cfg: Config, source_path: Path, *, kind: str = "article") -> int:
    """Ingest one file. Returns the document id.

    Re-ingesting an already-known source path is a no-op (returns the
    existing id). Use a fresh source path to force a re-ingest.
    """
    source_path = source_path.resolve()
    fmt = detect_format(source_path)
    if fmt not in INGESTERS:
        raise UnsupportedFormatError(f"Unsupported format for {source_path.name}")

    with db.transaction(cfg) as conn:
        existing = db.find_document_by_source(conn, str(source_path))
        if existing is not None:
            return int(existing["id"])

    normalized: NormalizedDoc = INGESTERS[fmt](source_path)
    language = detect_language(normalized.body, default=cfg.default_language)

    with db.transaction(cfg) as conn:
        doc_id = db.insert_document(
            conn,
            kind=kind,
            title=normalized.title,
            source_path=str(source_path),
            normalized_path=None,  # filled in below once we know the doc_id
            language=language,
            metadata=normalized.metadata,
        )
        normalized_path = write_normalized(
            out_dir=cfg.normalized_dir,
            doc_id=doc_id,
            doc=normalized,
            language=language,
        )
        conn.execute(
            "UPDATE documents SET normalized_path = ? WHERE id = ?;",
            (str(normalized_path), doc_id),
        )

    return doc_id


def iter_supported(path: Path) -> Iterable[Path]:
    """Yield supported files under `path` (recursively if a directory)."""
    if path.is_file():
        if path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path
        return
    for child in sorted(path.rglob("*")):
        if child.is_file() and child.suffix.lower() in SUPPORTED_SUFFIXES:
            yield child
