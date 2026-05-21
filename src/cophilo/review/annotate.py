"""Insert (and strip) cophilo review comments in a source file.

Review remarks are written *into the file* as comments in a syntax
appropriate to the file type, every physical line carrying a stable sentinel
(``cophilo-review``) so the whole pass is:

* **visually distinct** — comments never look like prose;
* **non-rendering** — ``%`` in LaTeX, ``<!-- … -->`` in Markdown/HTML, ``#``
  in scripts: the document still compiles/renders unchanged;
* **reversible & idempotent** — stripping removes exactly the sentinel lines,
  so re-reviewing replaces the prior pass and original lines are never
  touched.

A comment is placed on its own line(s) immediately *before* the line it
refers to (like a margin note), so anchoring survives editing better than a
trailing annotation would.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from cophilo.review.schemas import FileReview, ReviewComment

SENTINEL = "cophilo-review"
_WRAP = 84

_KIND_TAG = {
    "strength": "STRENGTH",
    "weakness": "WEAKNESS",
    "question": "QUESTION",
    "suggestion": "SUGGEST",
    "clarity": "CLARITY",
}


@dataclass(frozen=True)
class CommentSyntax:
    """How a given file type wraps a single physical comment line.

    ``lead``/``tail`` bracket the payload; ``probe`` is what a stripped line
    must start with to count as ours (``tail`` is checked separately).
    """

    lead: str
    tail: str
    probe: str

    def line(self, payload: str) -> str:
        return f"{self.lead}{payload}{self.tail}".rstrip()

    def is_ours(self, raw: str) -> bool:
        s = raw.strip()
        return s.startswith(self.probe) and (not self.tail or s.endswith(self.tail.strip()))


# Single-line `%` comments (LaTeX & friends).
_TEX = CommentSyntax(lead=f"% {SENTINEL} | ", tail="", probe=f"% {SENTINEL}")
# HTML comments — invisible in rendered Markdown/HTML, one self-closing
# comment per physical line so stripping stays line-based.
_HTML = CommentSyntax(lead=f"<!-- {SENTINEL} | ", tail=" -->", probe=f"<!-- {SENTINEL}")
# `#` line comments (scripts, YAML, TOML, …).
_HASH = CommentSyntax(lead=f"# {SENTINEL} | ", tail="", probe=f"# {SENTINEL}")
# Plain text has no comment syntax: use a loud, unmistakably-not-prose marker.
_TEXT = CommentSyntax(lead=f">>> {SENTINEL} | ", tail="", probe=f">>> {SENTINEL}")

_BY_SUFFIX: dict[str, CommentSyntax] = {}
for _suf in (".tex", ".sty", ".cls", ".ltx"):
    _BY_SUFFIX[_suf] = _TEX
for _suf in (".md", ".markdown", ".mdown", ".mkd", ".html", ".htm", ".xml"):
    _BY_SUFFIX[_suf] = _HTML
for _suf in (".py", ".sh", ".bash", ".zsh", ".rb", ".pl", ".r", ".jl",
             ".yaml", ".yml", ".toml", ".ini", ".cfg"):
    _BY_SUFFIX[_suf] = _HASH


def syntax_for(suffix: str) -> CommentSyntax:
    """Pick a comment syntax from a file suffix (``.tex`` …); text fallback."""
    return _BY_SUFFIX.get(suffix.lower(), _TEXT)


def newline_of(text: str) -> str:
    """The file's dominant line ending, so we don't silently rewrite CRLF."""
    return "\r\n" if "\r\n" in text else "\n"


def strip_lines(lines: list[str], syntax: CommentSyntax) -> list[str]:
    """Drop every cophilo review line, restoring the original source."""
    return [ln for ln in lines if not syntax.is_ours(ln)]


def _wrap(prefix: str, body: str) -> list[str]:
    """Wrap ``body`` to a column, repeating ``prefix`` on continuations so
    each physical line independently matches the strip probe."""
    width = max(20, _WRAP - len(prefix))
    chunks = textwrap.wrap(body, width=width) or [""]
    return [f"{prefix}{chunks[0]}"] + [f"{'·' * len(prefix)}{c}" for c in chunks[1:]]


def _comment_block(c: ReviewComment, syntax: CommentSyntax) -> list[str]:
    tag = _KIND_TAG.get(c.kind, c.kind.upper())
    return [syntax.line(p) for p in _wrap(f"[{tag}] ", c.comment.strip())]


def _banner(summary: str, general: list[ReviewComment], syntax: CommentSyntax) -> list[str]:
    rule = "─" * 60
    out = [
        syntax.line(rule),
        syntax.line("CRITICAL REVIEW — honest pass. Delete these "
                    f"`{SENTINEL}` lines to clear."),
    ]
    for p in _wrap("[SUMMARY] ", summary.strip()):
        out.append(syntax.line(p))
    for c in general:
        out.extend(_comment_block(c, syntax))
    out.append(syntax.line(rule))
    return out


_COVERAGE_TAG = {
    "engaged": "ENGAGED",
    "partial": "PARTIAL",
    "skipped": "SKIPPED",
}


def _coverage_block(coverage, syntax: CommentSyntax) -> list[str]:
    """Render the propose-question-coverage verdicts as a trailing section.

    Closes the §2.2 loop: the reader sees, in the same review pass, which
    of the open questions that ``propose`` pre-flagged the draft engaged
    or skipped, with the line that supports the verdict."""
    if not coverage:
        return []
    rule = "─" * 60
    out = [syntax.line(rule), syntax.line("PROPOSE-QUESTION COVERAGE")]
    for cov in coverage:
        tag = _COVERAGE_TAG.get(cov.status, cov.status.upper())
        line_hint = f" (line {cov.evidence_line})" if cov.evidence_line else ""
        body = f"[{tag}] “{cov.question.strip()}”{line_hint} — {cov.evidence.strip()}"
        for p in _wrap("", body):
            out.append(syntax.line(p))
    out.append(syntax.line(rule))
    return out


def numbered_source(source: str) -> str:
    """The exact text shown to the reviewer: 1-indexed ``NNNN│ line``.

    The numbers are the anchors the model returns; they are not part of the
    document.
    """
    lines = source.splitlines()
    return "\n".join(f"{i:>4}│{ln}" for i, ln in enumerate(lines, start=1)) or "1│"


def clean_source(text: str, syntax: CommentSyntax) -> str:
    """``text`` with any prior review lines removed (what the model reviews)."""
    return "\n".join(strip_lines(text.splitlines(), syntax))


def _norm(s: str) -> str:
    """Whitespace-collapsed form, so a re-wrapped line still matches its
    anchor (line numbers are not the only anchor)."""
    return " ".join(s.split())


def _resolve_line(c: ReviewComment, src_lines: list[str], n: int) -> int:
    """The 1-indexed line this comment should sit before.

    Prefer the model's line number; but if the file was edited since the
    review was generated, that number now points at unrelated text. Fall
    back to the quoted ``anchor`` and re-find the line it was copied from.
    """
    line = c.line
    if not (1 <= line <= n):
        return 0  # general remark
    anchor = _norm(c.anchor)
    if not anchor or anchor in _norm(src_lines[line - 1]):
        return line  # number still good (or no anchor to check)
    for i, raw in enumerate(src_lines, start=1):
        if anchor in _norm(raw):
            return i  # drifted — re-anchored by quote
    return line  # quote gone too; keep the model's best guess


def kind_counts(review: FileReview, only: set[str] | None = None) -> dict[str, int]:
    """How many comments of each kind — for the CLI's one-line tally."""
    counts: dict[str, int] = {}
    for c in review.comments:
        if only is not None and c.kind not in only:
            continue
        counts[c.kind] = counts.get(c.kind, 0) + 1
    return counts


_PLURAL = {
    "strength": "strengths",
    "weakness": "weaknesses",
    "question": "questions",
    "suggestion": "suggestions",
    "clarity": "clarity notes",
}


def tally_phrase(review: FileReview, only: set[str] | None = None) -> str:
    """``"5 weaknesses, 2 questions, 1 strength"`` — empty string if none."""
    counts = kind_counts(review, only)
    order = ["weakness", "question", "suggestion", "clarity", "strength"]
    parts = []
    for k in order:
        n = counts.get(k, 0)
        if not n:
            continue
        label = _PLURAL[k] if n != 1 else k
        parts.append(f"{n} {label}")
    return ", ".join(parts)


def sidecar_markdown(
    filename: str,
    review: FileReview,
    only: set[str] | None = None,
) -> str:
    """A standalone ``<name>.review.md`` — for files no mark may touch."""
    anchored = [c for c in review.comments if c.line and (only is None or c.kind in only)]
    general = [
        c for c in review.comments if not c.line and (only is None or c.kind in only)
    ]
    out = [
        f"# Review of `{filename}`",
        "",
        "> " + review.summary.strip().replace("\n", "\n> "),
        "",
    ]
    if anchored:
        out += ["## Line-anchored remarks", ""]
        for c in sorted(anchored, key=lambda c: c.line):
            tag = _KIND_TAG.get(c.kind, c.kind.upper())
            quote = f'  ·  “{c.anchor.strip()}”' if c.anchor.strip() else ""
            out.append(f"- **[{tag}] line {c.line}**{quote}")
            out.append(f"  {c.comment.strip()}")
        out.append("")
    if general:
        out += ["## General remarks", ""]
        for c in general:
            tag = _KIND_TAG.get(c.kind, c.kind.upper())
            out.append(f"- **[{tag}]** {c.comment.strip()}")
        out.append("")
    coverage = getattr(review, "propose_question_coverage", None) or []
    if coverage:
        out += ["## Propose-question coverage", ""]
        for cov in coverage:
            tag = _COVERAGE_TAG.get(cov.status, cov.status.upper())
            line_hint = f" (line {cov.evidence_line})" if cov.evidence_line else ""
            out.append(f"- **[{tag}]** “{cov.question.strip()}”{line_hint}")
            if cov.evidence.strip():
                out.append(f"  {cov.evidence.strip()}")
        out.append("")
    out.append(f"<!-- generated by `cophilo review`; {SENTINEL} sidecar -->")
    return "\n".join(out) + "\n"


def annotate(
    text: str,
    review: FileReview,
    syntax: CommentSyntax,
    *,
    only: set[str] | None = None,
) -> str:
    """Return ``text`` with the review woven in as marked comment lines.

    Prior review lines are stripped first, so this is idempotent and only
    ever adds/removes comment lines — never the document's own lines. With
    ``only``, keep just those comment ``kind``s (the summary stays).
    """
    nl = newline_of(text)
    src_lines = strip_lines(text.splitlines(), syntax)
    n = len(src_lines)

    by_line: dict[int, list[ReviewComment]] = {}
    general: list[ReviewComment] = []
    for c in review.comments:
        if only is not None and c.kind not in only:
            continue
        resolved = _resolve_line(c, src_lines, n)
        (by_line.setdefault(resolved, []) if resolved else general).append(c)

    out: list[str] = _banner(review.summary, general, syntax)
    for i, line in enumerate(src_lines, start=1):
        for c in by_line.get(i, []):
            out.extend(_comment_block(c, syntax))
        out.append(line)
    # Coverage report tails the document so it doesn't visually interfere
    # with the line-anchored remarks; it is still part of the same pass and
    # is cleared the same way (every line carries the sentinel).
    coverage = getattr(review, "propose_question_coverage", None) or []
    if coverage:
        out.extend(_coverage_block(coverage, syntax))

    rendered = nl.join(out)
    if text.endswith("\n"):
        rendered += nl
    return rendered
