"""Propose an article from the user's notes, and (on acceptance) carve out a
draft folder for it.

``propose_articles`` reads every note document, hands them to Claude, and
asks whether a coherent article hides in a subset. ``accept_proposal`` then
creates ``data/corpus/drafts/<slug>/``, **moves** the chosen note files into
it, repoints their DB rows, and drops an ``OUTLINE.md`` so ``compose`` has
the thesis/outline to work from.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
from slugify import slugify

from cophilo.config import Config
from cophilo.db import models as db
from cophilo.draft.schemas import ArticleProposal, ArticleProposals
from cophilo.extract.claude import LLMClient, make_client

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_NOTE_CHAR_CAP = 4000
_MAX_NOTES = 80
OUTLINE_NAME = "OUTLINE.md"


def load_prompt(language: str, name: str) -> str:
    candidate = _PROMPTS_DIR / language / f"{name}.md"
    if not candidate.exists():
        candidate = _PROMPTS_DIR / "en" / f"{name}.md"
    return candidate.read_text(encoding="utf-8")


@dataclass(frozen=True)
class NoteDoc:
    doc_id: int
    title: str
    text: str
    source_path: Path


def _read_note_text(row, char_cap: int) -> str:
    """Prefer the normalized markdown body; fall back to the source file."""
    for key in ("normalized_path", "source_path"):
        p = row[key]
        if not p:
            continue
        path = Path(p)
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = frontmatter.loads(raw).content if key == "normalized_path" else raw
        text = text.strip()
        return text if len(text) <= char_cap else text[:char_cap].rstrip() + " […]"
    return ""


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def collect_notes(
    cfg: Config, *, char_cap: int = _NOTE_CHAR_CAP, max_notes: int = _MAX_NOTES
) -> list[NoteDoc]:
    """All note documents not already pulled into a draft folder."""
    with db.transaction(cfg) as conn:
        rows = db.list_documents(conn, kind="note")
    notes: list[NoteDoc] = []
    for row in sorted(rows, key=lambda r: int(r["id"])):
        src = Path(row["source_path"])
        if _is_within(src, cfg.corpus_drafts_dir):
            continue  # already belongs to a draft
        text = _read_note_text(row, char_cap)
        if not text:
            continue
        notes.append(
            NoteDoc(
                doc_id=int(row["id"]),
                title=(row["title"] or src.stem),
                text=text,
                source_path=src,
            )
        )
    return notes[:max_notes]


def format_notes(notes: list[NoteDoc]) -> str:
    if not notes:
        return "(no notes available)"
    return "\n\n".join(
        f"[note {n.doc_id}] {n.title}\n{n.text}\n---" for n in notes
    )


@dataclass(frozen=True)
class ProposeResult:
    proposals: list[ArticleProposal]
    notes_by_id: dict[int, NoteDoc]
    notes_considered: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    proposals_raw: list[ArticleProposal] = field(default_factory=list)


def propose_articles(
    cfg: Config,
    *,
    client: LLMClient | None = None,
    language: str | None = None,
    max_notes: int = _MAX_NOTES,
    char_cap: int = _NOTE_CHAR_CAP,
) -> ProposeResult:
    """Ask Claude whether a coherent article hides in the user's notes.

    ``client`` is injectable so tests run without the API. With no notes the
    call is skipped entirely (no tokens spent)."""
    notes = collect_notes(cfg, char_cap=char_cap, max_notes=max_notes)
    notes_by_id = {n.doc_id: n for n in notes}
    if not notes:
        return ProposeResult([], {}, 0)

    language = language or cfg.default_language
    if client is None:
        client = make_client(cfg)

    template = load_prompt(language, "propose")
    system = template.format(notes=format_notes(notes), language=language)
    user = (
        "Decide whether a coherent article can be written from a subset of "
        "the notes above. Return JSON conforming to ArticleProposals."
    )
    result = client.call(
        model=cfg.claude_model_hard,
        system=system,
        user=user,
        response_model=ArticleProposals,
        max_tokens=4000,
    )
    parsed: ArticleProposals = result.parsed  # type: ignore[assignment]

    # Keep only proposals that reference notes we actually hold.
    cleaned: list[ArticleProposal] = []
    for p in parsed.proposals:
        valid_ids = [i for i in p.note_ids if i in notes_by_id]
        if valid_ids:
            cleaned.append(p.model_copy(update={"note_ids": valid_ids}))

    return ProposeResult(
        proposals=cleaned,
        notes_by_id=notes_by_id,
        notes_considered=len(notes),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
        proposals_raw=list(parsed.proposals),
    )


def _unique_dir(parent: Path, slug: str) -> Path:
    candidate = parent / slug
    n = 2
    while candidate.exists():
        candidate = parent / f"{slug}-{n}"
        n += 1
    return candidate


def _render_outline(proposal: ArticleProposal, moved: list[tuple[int, str]]) -> str:
    post = frontmatter.Post(
        "",
        title=proposal.title,
        slug=proposal.slug,
        kind="draft",
        created=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    body: list[str] = [f"# {proposal.title}", ""]
    body += ["## Thesis", "", proposal.thesis.strip(), ""]
    body += ["## Why these notes cohere", "", proposal.rationale.strip(), ""]
    body += ["## Outline", ""]
    body += [f"{i}. {h}" for i, h in enumerate(proposal.outline, 1)] or ["(none)"]
    body += ["", "## Open questions", ""]
    body += [f"- {q}" for q in proposal.open_questions] or ["(none)"]
    body += ["", "## Source notes", ""]
    body += [f"- `[note {nid}]` → `{name}`" for nid, name in moved] or ["(none)"]
    post.content = "\n".join(body) + "\n"
    return frontmatter.dumps(post) + "\n"


def accept_proposal(
    cfg: Config, proposal: ArticleProposal, notes_by_id: dict[int, NoteDoc]
) -> Path:
    """Create ``drafts/<slug>/``, move the proposal's notes into it, repoint
    their DB rows, and write ``OUTLINE.md``. Returns the draft folder."""
    cfg.corpus_drafts_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(proposal.slug or proposal.title, max_length=60) or "draft"
    draft_dir = _unique_dir(cfg.corpus_drafts_dir, slug)
    draft_dir.mkdir(parents=True)

    moved: list[tuple[int, Path]] = []
    for nid in proposal.note_ids:
        nd = notes_by_id.get(nid)
        if nd is None or not nd.source_path.exists():
            continue
        dest = draft_dir / nd.source_path.name
        k = 2
        while dest.exists():
            dest = draft_dir / f"{nd.source_path.stem}-{k}{nd.source_path.suffix}"
            k += 1
        shutil.move(str(nd.source_path), str(dest))
        moved.append((nid, dest))

    if moved:
        with db.transaction(cfg) as conn:
            for nid, dest in moved:
                db.update_document_source(conn, nid, str(dest.resolve()))

    (draft_dir / OUTLINE_NAME).write_text(
        _render_outline(proposal, [(nid, p.name) for nid, p in moved]),
        encoding="utf-8",
    )
    return draft_dir
