"""Topic synthesis: retrieved bibliography + user topic → structured reading.

Reuses the extraction layer's Anthropic wrapper (typed ``messages.parse()``
with prompt caching) so the synthesis is schema-validated the same way the
concept/question passes are.

Syntheses are persisted to ``data/syntheses/<slug>.{json,md}`` so downstream
features (``draft --from-synthesis``) can reuse the curated bibliography and
the source-quality verdicts without re-billing PhilArchive / Claude.
"""

from __future__ import annotations

import hashlib
import json as _json
from dataclasses import dataclass
from pathlib import Path

from slugify import slugify

from cophilo.biblio.schemas import BiblioEntry, TopicSynthesis
from cophilo.config import Config
from cophilo.extract.claude import LLMClient, make_client

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_MAX_ABSTRACT_CHARS = 1200
_SLUG_MAX = 70


def load_prompt(language: str, name: str) -> str:
    """Load a biblio prompt template, falling back to English."""
    candidate = _PROMPTS_DIR / language / f"{name}.md"
    if not candidate.exists():
        candidate = _PROMPTS_DIR / "en" / f"{name}.md"
    return candidate.read_text(encoding="utf-8")


def format_entries(entries: list[BiblioEntry]) -> str:
    """Render entries as ``[i] title / meta / abstract / url`` blocks.

    No tier annotations — used by ``synthesize`` itself before the model has
    judged the corpus."""
    if not entries:
        return "(no works retrieved)"
    blocks: list[str] = []
    for i, e in enumerate(entries, start=1):
        meta = " — ".join(
            p for p in (e.authors_str() or None, e.journal, str(e.year) if e.year else None) if p
        )
        abstract = (e.abstract or "(no abstract)").strip()
        if len(abstract) > _MAX_ABSTRACT_CHARS:
            abstract = abstract[:_MAX_ABSTRACT_CHARS].rstrip() + " […]"
        header = f"[{i}] [{e.external_id}] {e.title}"
        if meta:
            header += f"\n    {meta}"
        blocks.append(f"{header}\n    {abstract}\n    {e.url}")
    return "\n\n".join(blocks)


def format_entries_with_tiers(
    entries: list[BiblioEntry], synthesis: TopicSynthesis
) -> str:
    """Like ``format_entries`` but tags each entry with its tier and cite-as
    posture so the draft prompt knows which to lead with and which to
    quarantine. ``do_not_cite`` entries are dropped entirely."""
    judgements = {j.external_id: j for j in synthesis.source_judgements}
    blocks: list[str] = []
    i = 0
    for e in entries:
        judgement = judgements.get(e.external_id)
        if judgement and judgement.cite_as == "do_not_cite":
            continue
        i += 1
        meta = " — ".join(
            p for p in (e.authors_str() or None, e.journal, str(e.year) if e.year else None) if p
        )
        abstract = (e.abstract or "(no abstract)").strip()
        if len(abstract) > _MAX_ABSTRACT_CHARS:
            abstract = abstract[:_MAX_ABSTRACT_CHARS].rstrip() + " […]"
        if judgement:
            tier_tag = f"[{judgement.tier.upper()} — cite as {judgement.cite_as}]"
        else:
            tier_tag = "[UNJUDGED — treat as background only]"
        header = f"[{i}] [{e.external_id}] {tier_tag} {e.title}"
        if meta:
            header += f"\n    {meta}"
        blocks.append(f"{header}\n    {abstract}\n    {e.url}")
    if not blocks:
        return "(no citable works retrieved — every entry was marked do_not_cite)"
    return "\n\n".join(blocks)


def format_missing_canonical(synthesis: TopicSynthesis) -> str:
    """Render the missing-canonical list so the draft prompt can use it to
    seed ``[citation needed]`` flags in the prose. Returns a sentinel when
    empty so the prompt is always concrete."""
    if not synthesis.missing_canonical:
        return "(no missing canonical literature flagged)"
    lines = []
    for m in synthesis.missing_canonical:
        hint = f" ({m.work_hint})" if m.work_hint else ""
        lines.append(f"- {m.author}{hint}: {m.why}")
    return "\n".join(lines)


@dataclass(frozen=True)
class SynthesisResult:
    synthesis: TopicSynthesis
    entries: list[BiblioEntry]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


def synthesize_topic(
    cfg: Config,
    topic: str,
    entries: list[BiblioEntry],
    *,
    client: LLMClient | None = None,
    language: str | None = None,
) -> SynthesisResult:
    """Ask Claude to synthesize the literature on ``topic``.

    ``client`` is injectable so tests can run without the API.
    """
    topic = topic.strip()
    if not topic:
        raise ValueError("topic must be non-empty")
    language = language or cfg.default_language
    if client is None:
        client = make_client(cfg)

    template = load_prompt(language, "synthesize")
    system = template.format(entries=format_entries(entries), language=language)
    user = (
        "The user's topic:\n\n"
        f"{topic}\n\n"
        "Synthesize the literature above for this topic. "
        "Return JSON conforming to TopicSynthesis."
    )

    result = client.call(
        model=cfg.claude_model_hard,
        system=system,
        user=user,
        response_model=TopicSynthesis,
        max_tokens=4000,
    )
    return SynthesisResult(
        synthesis=result.parsed,  # type: ignore[arg-type]
        entries=entries,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
    )


def synthesis_slug(topic: str) -> str:
    """A short, stable slug for the topic. Same topic → same filename."""
    base = slugify(topic, max_length=_SLUG_MAX) or "synthesis"
    # Append a short hash so two different topics with identical slug prefixes
    # don't overwrite each other.
    digest = hashlib.sha1(topic.strip().encode("utf-8")).hexdigest()[:6]
    return f"{base}-{digest}"


def synthesis_paths(cfg: Config, topic: str) -> tuple[Path, Path]:
    """Canonical (json, md) locations for a topic's synthesis."""
    slug = synthesis_slug(topic)
    return (
        cfg.syntheses_dir / f"{slug}.json",
        cfg.syntheses_dir / f"{slug}.md",
    )


def save_synthesis(
    cfg: Config,
    topic: str,
    query: str,
    synthesis: TopicSynthesis,
    entries: list[BiblioEntry],
    *,
    venues: list[dict] | None = None,
) -> tuple[Path, Path]:
    """Persist a synthesis to ``data/syntheses/<slug>.{json,md}``.

    Returns the (json_path, md_path) pair so callers can echo them. When
    ``venues`` is omitted, candidate venues are looked up automatically
    against the local memory index — silently skipped if not available.
    """
    cfg.syntheses_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = synthesis_paths(cfg, topic)
    if venues is None:
        venues = candidate_venues(cfg, topic)
    payload = {
        "topic": topic.strip(),
        "query": query.strip(),
        "synthesis": synthesis.model_dump(),
        "entries": [e.model_dump() for e in entries],
        "venues": venues,
    }
    json_path.write_text(
        _json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(
        render_markdown(topic, query, synthesis, entries, venues=venues),
        encoding="utf-8",
    )
    return json_path, md_path


@dataclass(frozen=True)
class LoadedSynthesis:
    """A `TopicSynthesis` round-tripped from disk with its bibliography."""

    topic: str
    query: str
    synthesis: TopicSynthesis
    entries: list[BiblioEntry]
    venues: list[dict]


def load_synthesis(path: Path) -> LoadedSynthesis:
    """Load a saved synthesis JSON. Round-trips through pydantic so the
    schema is validated; new optional fields default cleanly on old files."""
    data = _json.loads(Path(path).read_text(encoding="utf-8"))
    return LoadedSynthesis(
        topic=data.get("topic", ""),
        query=data.get("query", ""),
        synthesis=TopicSynthesis.model_validate(data["synthesis"]),
        entries=[BiblioEntry.model_validate(e) for e in data.get("entries", [])],
        venues=data.get("venues") or [],
    )


def candidate_venues(cfg: Config, topic: str, limit: int = 5) -> list[dict]:
    """Best-effort venue match against the local journals catalog.

    Returns ``[]`` quietly when the memory index isn't built (no
    ``data/journals.yaml`` or no derived sqlite-vec store yet) — this is a
    cross-link, not a hard dependency."""
    try:
        from cophilo.memory import search as memory_search
    except ImportError:  # pragma: no cover — memory extra not installed
        return []
    try:
        return memory_search(cfg, topic, limit=limit)
    except (FileNotFoundError, ValueError):
        return []
    except Exception:  # pragma: no cover — never let a venue lookup break synthesize
        return []


def render_markdown(
    topic: str,
    query: str,
    synthesis: TopicSynthesis,
    entries: list[BiblioEntry],
    *,
    venues: list[dict] | None = None,
) -> str:
    s = synthesis
    lines: list[str] = []
    lines.append("# Bibliography synthesis")
    lines.append("")
    lines.append(f"**Topic.** {topic.strip()}")
    lines.append("")
    lines.append(f"**PhilArchive query.** `{query}` — {len(entries)} works retrieved.")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(s.overview.strip())
    lines.append("")
    if s.corpus_caveats.strip():
        lines.append("## Corpus caveats")
        lines.append("")
        lines.append(s.corpus_caveats.strip())
        lines.append("")
    lines.append("## Big questions")
    lines.append("")
    lines.extend(f"- {q}" for q in s.big_questions)
    if not s.big_questions:
        lines.append("- (none identified)")
    lines.append("")
    lines.append("## Smaller questions")
    lines.append("")
    lines.extend(f"- {q}" for q in s.small_questions)
    if not s.small_questions:
        lines.append("- (none identified)")
    lines.append("")
    lines.append("## Key works")
    lines.append("")
    if s.key_works:
        for kw in s.key_works:
            who = f" — {kw.authors}" if kw.authors else ""
            lines.append(f"- **{kw.title}**{who}. {kw.why}")
    else:
        lines.append("- (none identified)")
    lines.append("")
    if s.missing_canonical:
        lines.append("## Missing canonical literature")
        lines.append("")
        for m in s.missing_canonical:
            hint = f" ({m.work_hint})" if m.work_hint else ""
            lines.append(f"- **{m.author}**{hint} — {m.why}")
        lines.append("")
    if s.suggested_searches:
        lines.append("## Suggested follow-up searches")
        lines.append("")
        # Printed as runnable commands (REPORT.md §2.4 wish): copy-paste, not
        # backtick-quoted hints.
        lines.extend(
            f'- `cophilo biblio search "{q}"`' for q in s.suggested_searches
        )
        lines.append("")
    if s.source_judgements:
        # Sort by tier so the canonical references are visible at the top.
        order = {"canonical": 0, "peer_reviewed": 1, "speculative": 2, "off_topic": 3}
        judgements = sorted(s.source_judgements, key=lambda j: order.get(j.tier, 9))
        lines.append("## Source-quality verdicts")
        lines.append("")
        for j in judgements:
            lines.append(f"- **[{j.tier}]** `{j.external_id}` — cite as *{j.cite_as}*. {j.rationale}")
        lines.append("")
    if venues:
        # §2.3 — bridge synthesize → memory search so the "where does this go"
        # step lives next to the "what is the landscape" step.
        lines.append("## Candidate venues")
        lines.append("")
        lines.append(
            "_Semantic match against the local journals catalog. Use "
            "`cophilo memory search` for a fuller list._"
        )
        lines.append("")
        for v in venues:
            oa = " [OA]" if v.get("open_access") else ""
            score = v.get("score")
            score_str = f" (score {score})" if score is not None else ""
            scope = v.get("scope") or ""
            url = v.get("url") or ""
            link = f" — <{url}>" if url else ""
            scope_line = f" — {scope}" if scope else ""
            lines.append(f"- **{v['name']}**{oa}{score_str}{scope_line}{link}")
        lines.append("")
    lines.append("## Retrieved bibliography")
    lines.append("")
    for i, e in enumerate(entries, start=1):
        lines.append(f"{i}. {e.citation()} — <{e.url}>")
    lines.append("")
    return "\n".join(lines)
