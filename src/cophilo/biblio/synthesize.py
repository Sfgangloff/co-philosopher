"""Topic synthesis: retrieved bibliography + user topic → structured reading.

Reuses the extraction layer's Anthropic wrapper (typed ``messages.parse()``
with prompt caching) so the synthesis is schema-validated the same way the
concept/question passes are.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cophilo.biblio.schemas import BiblioEntry, TopicSynthesis
from cophilo.config import Config
from cophilo.extract.claude import LLMClient, make_client

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_MAX_ABSTRACT_CHARS = 1200


def load_prompt(language: str, name: str) -> str:
    """Load a biblio prompt template, falling back to English."""
    candidate = _PROMPTS_DIR / language / f"{name}.md"
    if not candidate.exists():
        candidate = _PROMPTS_DIR / "en" / f"{name}.md"
    return candidate.read_text(encoding="utf-8")


def format_entries(entries: list[BiblioEntry]) -> str:
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
        header = f"[{i}] {e.title}"
        if meta:
            header += f"\n    {meta}"
        blocks.append(f"{header}\n    {abstract}\n    {e.url}")
    return "\n\n".join(blocks)


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


def render_markdown(
    topic: str, query: str, synthesis: TopicSynthesis, entries: list[BiblioEntry]
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
    if s.suggested_searches:
        lines.append("## Suggested follow-up searches")
        lines.append("")
        lines.extend(f"- `{q}`" for q in s.suggested_searches)
        lines.append("")
    lines.append("## Retrieved bibliography")
    lines.append("")
    for i, e in enumerate(entries, start=1):
        lines.append(f"{i}. {e.citation()} — <{e.url}>")
    lines.append("")
    return "\n".join(lines)
