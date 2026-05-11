"""Common normalization helpers shared by all ingesters.

Each ingester returns a NormalizedDoc; normalize.py handles language
detection, frontmatter assembly, and writing to data/normalized/.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
from lingua import IsoCode639_1, LanguageDetectorBuilder
from slugify import slugify

# We only need EN/FR for now. Restricting the detector improves accuracy on
# short notes.
_DETECTOR = (
    LanguageDetectorBuilder.from_iso_codes_639_1(IsoCode639_1.EN, IsoCode639_1.FR)
    .with_preloaded_language_models()
    .build()
)

_LANG_TO_CODE = {"ENGLISH": "en", "FRENCH": "fr"}


@dataclass
class NormalizedDoc:
    """Output of an ingester. The `body` is markdown without frontmatter."""

    title: str | None
    body: str
    source_path: Path
    source_format: str  # 'pdf' | 'docx' | 'tex' | 'md'
    metadata: dict[str, Any] = field(default_factory=dict)


def detect_language(text: str, default: str = "en") -> str:
    """Return 'en' or 'fr'. Falls back to default for very short or
    indeterminate text."""
    sample = text[:4000].strip()
    if len(sample) < 40:
        return default
    detected = _DETECTOR.detect_language_of(sample)
    if detected is None:
        return default
    return _LANG_TO_CODE.get(detected.name, default)


_WS_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def collapse_whitespace(text: str) -> str:
    """Collapse runs of spaces/tabs and limit consecutive blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Don't collapse leading whitespace (preserves indented code/quotes).
    out_lines = []
    for line in text.split("\n"):
        # Preserve leading spaces, collapse internal runs.
        stripped_lead = len(line) - len(line.lstrip(" \t"))
        lead, rest = line[:stripped_lead], line[stripped_lead:]
        out_lines.append(lead + _WS_RE.sub(" ", rest).rstrip())
    out = "\n".join(out_lines)
    out = _BLANKLINES_RE.sub("\n\n", out)
    return out.strip() + "\n"


def derive_doc_slug(source_path: Path, title: str | None) -> str:
    base = title or source_path.stem
    return slugify(base, max_length=80) or slugify(source_path.stem) or "untitled"


def write_normalized(
    *,
    out_dir: Path,
    doc_id: int,
    doc: NormalizedDoc,
    language: str,
) -> Path:
    """Write the normalized markdown with YAML frontmatter and return its path.

    Filename: ``<doc_id>-<slug>.md`` so the DB primary key prefixes the file
    for easy grep, but the slug stays human-readable.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = derive_doc_slug(doc.source_path, doc.title)
    path = out_dir / f"{doc_id:04d}-{slug}.md"

    post = frontmatter.Post(
        doc.body,
        title=doc.title,
        source=str(doc.source_path),
        source_format=doc.source_format,
        language=language,
        **{k: v for k, v in doc.metadata.items() if v is not None},
    )
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return path


def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".pdf": "pdf",
        ".docx": "docx",
        ".tex": "tex",
        ".md": "md",
        ".markdown": "md",
    }.get(suffix, "")
