"""Format dispatcher: pick the right ingester for a file and orchestrate the
write to disk + DB insert.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from cophilo.config import Config
from cophilo.db import models as db
from cophilo.ingest.docx import ingest_docx
from cophilo.ingest.md import ingest_md
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
    "md": ingest_md,
}

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".tex", ".md", ".markdown"}


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


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def infer_kind(cfg: Config, path: Path) -> str:
    """Infer a document kind from which corpus subfolder ``path`` sits in.

    ``corpus/notes/**`` → ``note``; everything else (including
    ``corpus/articles/**`` and loose files) → ``article``. Files under
    ``corpus/drafts/**`` are excluded upstream and never reach here.
    """
    if _is_within(path, cfg.corpus_notes_dir):
        return "note"
    return "article"


@dataclass(frozen=True)
class IngestOutcome:
    path: Path
    kind: str
    status: str  # 'new' | 'existing' | 'failed'
    doc_id: int | None = None
    error: str | None = None


def ingest_tree(
    cfg: Config,
    root: Path,
    *,
    kind_override: str | None = None,
) -> list[IngestOutcome]:
    """Ingest every supported file under ``root``, skipping ``corpus/drafts``.

    Already-ingested files (matched by source path) are reported as
    ``existing`` and not re-processed — so a bare ``cophilo ingest`` only
    picks up what is new. ``kind`` is inferred per file from the corpus
    subfolder unless ``kind_override`` forces it.
    """
    # `cophilo backup` drops a marker README at the corpus root (it must live
    # there so the backup git repo tracks it). It is backup metadata, not
    # philosophy — never ingest it as an [article].
    backup_readme = (cfg.corpus_dir / "README.md").resolve()

    outcomes: list[IngestOutcome] = []
    for f in iter_supported(root):
        if _is_within(f, cfg.corpus_drafts_dir):
            continue  # drafts are work-in-progress, not corpus material
        if f.resolve() == backup_readme:
            continue  # backup metadata, not corpus material
        kind = kind_override or infer_kind(cfg, f)
        resolved = str(f.resolve())
        with db.transaction(cfg) as conn:
            existing = db.find_document_by_source(conn, resolved)
        if existing is not None:
            outcomes.append(
                IngestOutcome(f, kind, "existing", doc_id=int(existing["id"]))
            )
            continue
        try:
            doc_id = ingest_file(cfg, f, kind=kind)
            outcomes.append(IngestOutcome(f, kind, "new", doc_id=doc_id))
        except UnsupportedFormatError as e:
            outcomes.append(IngestOutcome(f, kind, "failed", error=str(e)))
        except Exception as e:  # pragma: no cover — surface unexpected failures
            outcomes.append(IngestOutcome(f, kind, "failed", error=str(e)))
    return outcomes
