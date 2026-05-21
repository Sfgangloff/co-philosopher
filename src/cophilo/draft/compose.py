"""Compose an ``article.tex`` for a draft folder.

Reads the notes (and ``OUTLINE.md``) that ``propose`` moved into a
``drafts/<slug>/`` folder, retrieves a fresh PhilArchive bibliography from
the thesis, asks Claude to draft the article grounded in both, and writes
``article.tex`` next to the notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from cophilo.biblio import philarchive
from cophilo.biblio.schemas import BiblioEntry, TopicSynthesis
from cophilo.biblio.synthesize import (
    LoadedSynthesis,
    format_entries,
    format_entries_with_tiers,
    format_missing_canonical,
    load_synthesis,
    synthesis_paths,
)
from cophilo.config import Config
from cophilo.db import models as db
from cophilo.draft.propose import OUTLINE_NAME, load_prompt
from cophilo.draft.render_tex import render_tex
from cophilo.draft.schemas import ArticleDraft
from cophilo.extract.claude import LLMClient, make_client
from cophilo.ingest.normalize import detect_language

TEX_NAME = "article.tex"
_QUERY_FALLBACK_CHARS = 240


def _extract_section(md: str, heading: str) -> str:
    """Return the text under a ``## <heading>`` block (until the next ##)."""
    m = re.search(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)",
        md,
        flags=re.MULTILINE | re.DOTALL,
    )
    return m.group(1).strip() if m else ""


def _load_notes(draft_dir: Path) -> list[tuple[str, str]]:
    """(title, body) for every note .md in the folder, excluding OUTLINE.md."""
    notes: list[tuple[str, str]] = []
    for path in sorted(draft_dir.glob("*.md")):
        if path.name == OUTLINE_NAME:
            continue
        post = frontmatter.loads(path.read_text(encoding="utf-8", errors="replace"))
        title = (post.get("title") or path.stem).strip()
        body = post.content.strip()
        if body:
            notes.append((title, body))
    return notes


@dataclass(frozen=True)
class ComposeResult:
    draft: ArticleDraft
    entries: list[BiblioEntry]
    tex_path: Path
    query: str
    language: str
    synthesis_used: Path | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def _discover_synthesis(cfg: Config, thesis: str) -> Path | None:
    """Look for a saved synthesis whose topic matches the draft's thesis.

    The synthesis filename is derived from the topic via ``synthesis_slug``
    so a topic→thesis match is a deterministic path check. If no exact
    match, fall through to None (don't second-guess; the user can pass
    --from-synthesis explicitly)."""
    if not thesis.strip():
        return None
    json_path, _ = synthesis_paths(cfg, thesis)
    return json_path if json_path.exists() else None


def compose_draft(
    cfg: Config,
    draft_dir: Path,
    *,
    client: LLMClient | None = None,
    language: str | None = None,
    query: str | None = None,
    limit: int = 30,
    fetcher: philarchive.Fetcher | None = None,
    from_synthesis: Path | None = None,
) -> ComposeResult:
    """Draft ``article.tex`` for ``draft_dir``.

    ``client``/``fetcher`` are injectable so tests need no API or network.

    When ``from_synthesis`` (or an auto-discovered synthesis on disk) is
    available, the bibliography is **reused** rather than re-queried, and the
    source-quality verdicts the synthesis already produced are passed into
    the draft prompt so it can refuse to dress speculative grey literature
    as scholarly convergence (REPORT.md §1.8 / §2.1).
    """
    draft_dir = draft_dir.resolve()
    if not draft_dir.is_dir():
        raise ValueError(f"not a draft folder: {draft_dir}")

    notes = _load_notes(draft_dir)
    if not notes:
        raise ValueError(f"no notes (*.md) found in {draft_dir}")

    outline_path = draft_dir / OUTLINE_NAME
    thesis = outline_text = ""
    outline_title: str | None = None
    if outline_path.exists():
        post = frontmatter.loads(outline_path.read_text(encoding="utf-8"))
        outline_title = (post.get("title") or "").strip() or None
        thesis = _extract_section(post.content, "Thesis")
        outline_text = _extract_section(post.content, "Outline")

    notes_block = "\n\n".join(f"## {t}\n{b}" for t, b in notes)
    combined = "\n\n".join(b for _, b in notes)
    language = language or detect_language(combined, default=cfg.default_language)

    # Bibliography: prefer a saved synthesis (explicit > auto-discovered) over
    # a fresh PhilArchive query. The synthesis already carries the model's
    # tier verdicts and missing-canonical list — refetching would lose both.
    synthesis_path: Path | None = from_synthesis
    if synthesis_path is None:
        synthesis_path = _discover_synthesis(cfg, thesis or outline_title or "")

    loaded: LoadedSynthesis | None = None
    if synthesis_path is not None and synthesis_path.exists():
        loaded = load_synthesis(synthesis_path)

    if loaded is not None:
        entries = loaded.entries
        search_query = loaded.query or "(reused from synthesis)"
        synthesis_obj: TopicSynthesis | None = loaded.synthesis
    else:
        search_query = (
            (query or thesis or outline_title or combined[:_QUERY_FALLBACK_CHARS])
            .strip()
            .replace("\n", " ")
        )
        entries = philarchive.search(cfg, search_query, limit=limit, fetcher=fetcher)
        synthesis_obj = None

    if entries:
        with db.transaction(cfg) as conn:
            for e in entries:
                db.upsert_bibliography(
                    conn,
                    source=e.source,
                    external_id=e.external_id,
                    title=e.title,
                    authors=e.authors_str() or None,
                    journal=e.journal,
                    year=e.year,
                    abstract=e.abstract,
                    doi=e.doi,
                )

    if client is None:
        client = make_client(cfg)

    template = load_prompt(language, "draft")
    if synthesis_obj is not None:
        entries_block = format_entries_with_tiers(entries, synthesis_obj)
        missing_block = format_missing_canonical(synthesis_obj)
        caveats_block = (
            synthesis_obj.corpus_caveats.strip()
            or "(no corpus caveats flagged — bibliography is solid on the topic)"
        )
    else:
        entries_block = format_entries(entries)
        missing_block = "(no synthesis available — proceed cautiously with the abstracts you have)"
        caveats_block = "(no synthesis available — be honest about what the bibliography below cannot support)"

    system = template.format(
        thesis=thesis or "(none provided — infer from the notes)",
        outline=outline_text or "(none provided — propose your own structure)",
        notes=notes_block,
        entries=entries_block,
        missing_canonical=missing_block,
        corpus_caveats=caveats_block,
        language=language,
    )
    user = (
        "Draft the article from the notes, thesis, and bibliography above. "
        "Return JSON conforming to ArticleDraft."
    )
    result = client.call(
        model=cfg.claude_model_hard,
        system=system,
        user=user,
        response_model=ArticleDraft,
        max_tokens=8000,
    )
    draft: ArticleDraft = result.parsed  # type: ignore[assignment]

    tex_path = draft_dir / TEX_NAME
    tex_path.write_text(render_tex(draft, language=language), encoding="utf-8")

    return ComposeResult(
        draft=draft,
        entries=entries,
        tex_path=tex_path,
        query=search_query,
        language=language,
        synthesis_used=synthesis_path if loaded is not None else None,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
    )
