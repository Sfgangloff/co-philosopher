"""Orchestrate a critical-review pass over one file.

Reads the file, strips any prior review so the model never reviews its own
comments, asks Claude for an honest line-anchored critique, and writes the
annotated file back in place (or returns it for ``--dry-run``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from cophilo.config import Config
from cophilo.extract.claude import LLMClient, make_client
from cophilo.ingest.normalize import detect_language
from cophilo.review.annotate import (
    annotate,
    clean_source,
    newline_of,
    numbered_source,
    sidecar_markdown,
    strip_lines,
    syntax_for,
    tally_phrase,
)
from cophilo.review.schemas import CounterRound, FileReview

# Match the markdown bullet list under "## Open questions" in an OUTLINE.md.
# Stops at the next `## ` header or end of file.
_OPEN_QUESTIONS_RE = re.compile(
    r"^##\s+Open questions\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.MULTILINE)


def _find_open_questions(path: Path) -> list[str]:
    """Read sibling ``OUTLINE.md`` and pull out the open-questions bullets.

    Returns ``[]`` if no OUTLINE.md is next to the reviewed file, or if its
    "Open questions" section is empty / missing. The shape produced by
    ``propose`` is::

        ## Open questions

        - First question?
        - Second question?
    """
    outline = path.parent / "OUTLINE.md"
    if not outline.is_file():
        return []
    try:
        content = frontmatter.loads(
            outline.read_text(encoding="utf-8", errors="replace")
        ).content
    except Exception:  # pragma: no cover - malformed YAML, fall through
        return []
    m = _OPEN_QUESTIONS_RE.search(content)
    if m is None:
        return []
    questions = []
    for bullet in _BULLET_RE.finditer(m.group(1)):
        text = bullet.group(1).strip()
        # "(none)" placeholder from propose.py's _render_outline.
        if text and text != "(none)":
            questions.append(text)
    return questions


def _format_open_questions(questions: list[str]) -> str:
    if not questions:
        return "(none)"
    return "\n".join(f"- {q}" for q in questions)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_MAX_CHARS = 120_000


def load_prompt(language: str, name: str = "review") -> str:
    """Load a review prompt template, falling back to English."""
    candidate = _PROMPTS_DIR / language / f"{name}.md"
    if not candidate.exists():
        candidate = _PROMPTS_DIR / "en" / f"{name}.md"
    return candidate.read_text(encoding="utf-8")


@dataclass(frozen=True)
class ReviewResult:
    path: Path
    review: FileReview
    annotated_text: str
    language: str
    written: bool
    comment_count: int
    tally: str = ""
    sidecar: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def clear_review_comments(path: Path) -> bool:
    """Remove every cophilo review line from ``path``. No LLM, no network.

    Returns True if the file changed.
    """
    syntax = syntax_for(path.suffix)
    original = path.read_text(encoding="utf-8", errors="replace")
    nl = newline_of(original)
    cleaned = nl.join(strip_lines(original.splitlines(), syntax))
    if original.endswith("\n") and cleaned:
        cleaned += nl
    if cleaned == original:
        return False
    path.write_text(cleaned, encoding="utf-8")
    return True


def sidecar_path(path: Path) -> Path:
    """Where ``--format sidecar`` writes: ``<name>.review.md`` alongside the
    file, so a shared ``.tex`` never gets a single mark in it."""
    return path.with_name(path.name + ".review.md")


# --- §2.2 marginalia-as-conversation ----------------------------------------

MAX_ROUND_DEPTH = 3


def round_path(path: Path, round_index: int) -> Path:
    """``<name>.review-r2.md`` / ``…-r3.md`` for counter rounds. Round 1 is
    the original ``<name>.review.md``."""
    if round_index <= 1:
        return sidecar_path(path)
    return path.with_name(f"{path.name}.review-r{round_index}.md")


@dataclass(frozen=True)
class Exchange:
    """A single ``(critique, reply)`` pair the model will respond to."""

    original_comment: str
    user_reply: str
    anchor: str = ""


_REPLY_PREFIX_RE = re.compile(r"^\s*>\s*reply\s*:\s*(.+?)\s*$", re.IGNORECASE)


def _parse_sidecar_exchanges(text: str) -> list[Exchange]:
    """Parse a sidecar ``.review.md`` for (critique, reply) pairs.

    The contract is light: under each remark bullet, the user adds one or
    more ``> reply: …`` lines (markdown blockquote convention). Anything
    without a matching reply is skipped. Bullets are identified by the
    ``- **[KIND]`` prefix the sidecar renderer produces.
    """
    out: list[Exchange] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("- **[") and "]" in line:
            # Collect this bullet's continuation lines (anything indented or
            # blockquoted under it, up to the next bullet / heading / blank
            # gap of >1 line).
            bullet_anchor = ""
            # The sidecar bullet anchor looks like:
            #   - **[WEAKNESS] line 3**  ·  “Determinism is false.”
            m = re.search(r"·\s*[“\"](.+?)[”\"]", line)
            if m:
                bullet_anchor = m.group(1).strip()

            comment_parts: list[str] = []
            replies: list[str] = []
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if next_line.lstrip().startswith("- **[") or next_line.startswith("## "):
                    break
                rm = _REPLY_PREFIX_RE.match(next_line)
                if rm:
                    replies.append(rm.group(1).strip())
                elif next_line.strip():
                    # Indented (or unindented) prose under the bullet =
                    # part of the critique itself.
                    comment_parts.append(next_line.strip())
                i += 1
            comment = " ".join(comment_parts).strip()
            for reply in replies:
                out.append(
                    Exchange(
                        original_comment=comment,
                        user_reply=reply,
                        anchor=bullet_anchor,
                    )
                )
        else:
            i += 1
    return out


def _format_exchanges(exchanges: list[Exchange]) -> str:
    blocks = []
    for i, ex in enumerate(exchanges, start=1):
        a = f' (anchor: “{ex.anchor}”)' if ex.anchor else ""
        blocks.append(
            f"[exchange {i}]{a}\n"
            f"  critique: {ex.original_comment}\n"
            f"  reply:    {ex.user_reply}"
        )
    return "\n\n".join(blocks)


def _render_counter_round(
    filename: str, round_index: int, round: CounterRound
) -> str:
    """Markdown for a `.review-rN.md` file. Same shape as a sidecar review:
    a quoted summary, then one bullet per counter-reply with its verdict."""
    out = [
        f"# Counter-review of `{filename}` — round {round_index}",
        "",
        "> " + round.summary.strip().replace("\n", "\n> "),
        "",
        "## Counter-replies",
        "",
    ]
    for c in round.counters:
        anchor = f' · “{c.anchor.strip()}”' if c.anchor.strip() else ""
        out.append(f"- **[{c.verdict.upper()}]**{anchor}")
        out.append(f"  *Original:* {c.original_comment.strip()}")
        out.append(f"  *Reply:* {c.user_reply.strip()}")
        out.append(f"  *Counter:* {c.counter.strip()}")
        out.append("")
    out.append(
        f"<!-- generated by `cophilo review --respond-to`; round {round_index} -->"
    )
    return "\n".join(out) + "\n"


def _detect_prior_round(prior: Path) -> int:
    """Pull the round index out of ``…review-rN.md`` (default 1 for original)."""
    m = re.search(r"\.review-r(\d+)\.md$", prior.name)
    return int(m.group(1)) if m else 1


@dataclass(frozen=True)
class CounterResult:
    path: Path
    round: CounterRound
    round_index: int
    written: bool
    exchanges_count: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def respond_to_review(
    cfg: Config,
    path: Path,
    *,
    prior: Path,
    client: LLMClient | None = None,
    language: str | None = None,
    write: bool = True,
) -> CounterResult:
    """Second-round counter-pass over a prior review the user replied to.

    ``prior`` is a sidecar markdown (``…review.md`` or ``…review-rN.md``)
    that the user has annotated with ``> reply: …`` lines under any bullet
    they wish to engage. Bullets without a reply are skipped — the model
    only responds to what the user actually answered.
    """
    path = path.resolve()
    prior = prior.resolve()
    if not prior.is_file():
        raise ValueError(f"prior review not found: {prior}")
    prior_round = _detect_prior_round(prior)
    next_round = prior_round + 1
    if next_round > MAX_ROUND_DEPTH:
        raise ValueError(
            f"counter-rounds are capped at {MAX_ROUND_DEPTH}; this would be round {next_round}."
        )

    exchanges = _parse_sidecar_exchanges(prior.read_text(encoding="utf-8"))
    if not exchanges:
        raise ValueError(
            f"no '> reply:' lines found in {prior.name} — nothing to counter."
        )

    language = language or detect_language(path.read_text(encoding="utf-8", errors="replace"), default=cfg.default_language)
    if client is None:
        client = make_client(cfg)

    template = load_prompt(language, "respond")
    system = template.format(
        round_index=next_round,
        exchanges=_format_exchanges(exchanges),
        language=language,
    )
    user = (
        "Produce one counter-reply per exchange. Return JSON conforming to "
        "CounterRound."
    )
    result = client.call(
        model=cfg.claude_model_hard,
        system=system,
        user=user,
        response_model=CounterRound,
        max_tokens=4000,
    )
    counter: CounterRound = result.parsed  # type: ignore[assignment]

    out_path = round_path(path, next_round)
    rendered = _render_counter_round(path.name, next_round, counter)
    if write:
        out_path.write_text(rendered, encoding="utf-8")

    return CounterResult(
        path=out_path,
        round=counter,
        round_index=next_round,
        written=write,
        exchanges_count=len(exchanges),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
    )


def review_file(
    cfg: Config,
    path: Path,
    *,
    client: LLMClient | None = None,
    language: str | None = None,
    write: bool = True,
    only: set[str] | None = None,
    sidecar: bool = False,
) -> ReviewResult:
    """Critically review ``path`` and weave the remarks back into it.

    ``client`` is injectable so tests run without the API. ``write=False``
    leaves the file untouched and only returns the annotated text. ``only``
    keeps just those comment kinds; ``sidecar`` writes a separate
    ``<name>.review.md`` and never touches the source.
    """
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"not a file: {path}")

    syntax = syntax_for(path.suffix)
    original = path.read_text(encoding="utf-8", errors="replace")
    source = clean_source(original, syntax)
    if not source.strip():
        raise ValueError(f"nothing to review — {path} is empty")
    if len(source) > _MAX_CHARS:
        raise ValueError(
            f"{path} is too large to review in one pass "
            f"({len(source)} chars > {_MAX_CHARS}); split it first."
        )

    language = language or detect_language(source, default=cfg.default_language)
    if client is None:
        client = make_client(cfg)

    template = load_prompt(language)
    open_questions = _find_open_questions(path)
    system = template.format(
        filename=path.name,
        suffix=path.suffix or "(none)",
        language=language,
        open_questions=_format_open_questions(open_questions),
    )
    user = (
        "The file to review, with 1-indexed line numbers (the numbers and the "
        "│ are NOT part of the document — never quote them back):\n\n"
        f"{numbered_source(source)}\n\n"
        "Return JSON conforming to FileReview."
    )

    result = client.call(
        model=cfg.claude_model_hard,
        system=system,
        user=user,
        response_model=FileReview,
        max_tokens=8000,
    )
    review: FileReview = result.parsed  # type: ignore[assignment]

    if sidecar:
        annotated = sidecar_markdown(path.name, review, only)
        out_path = sidecar_path(path)
        if write:
            out_path.write_text(annotated, encoding="utf-8")
    else:
        annotated = annotate(original, review, syntax, only=only)
        out_path = path
        if write:
            path.write_text(annotated, encoding="utf-8")

    return ReviewResult(
        path=out_path,
        review=review,
        annotated_text=annotated,
        language=language,
        written=write,
        comment_count=len(review.comments),
        tally=tally_phrase(review, only),
        sidecar=sidecar,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
    )
