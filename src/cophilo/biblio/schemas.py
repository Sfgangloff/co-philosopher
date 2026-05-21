"""Typed shapes for bibliography search results and topic synthesis.

`BiblioEntry` is the normalized record returned by the PhilArchive client.
The `Synthesis*` models mirror the JSON Claude returns from the synthesize
prompt and are validated by ``messages.parse()`` (same pattern as the
extraction passes).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# A tier the model assigns to each retrieved bibliography entry. The two
# higher tiers are safe to cite as authority; the lower two are not. The
# `draft` prompt uses these to refuse the "convergence" rhetoric the
# philosopher's trial caught (REPORT.md §1.8 / §2.1).
SourceTier = Literal["canonical", "peer_reviewed", "speculative", "off_topic"]
CiteAs = Literal["primary", "supporting", "background", "do_not_cite"]


class BiblioEntry(BaseModel):
    """One bibliographic record normalized from a PhilArchive search hit."""

    source: str = "philarchive"
    external_id: str = Field(..., description="PhilArchive record id, e.g. 'LISFWD'.")
    title: str
    authors: list[str] = Field(default_factory=list)
    journal: str | None = None
    year: int | None = None
    abstract: str | None = None
    url: str
    doi: str | None = None

    def authors_str(self) -> str:
        return "; ".join(self.authors)

    def citation(self) -> str:
        bits = [self.authors_str() or "Unknown", f"“{self.title}”"]
        if self.journal:
            bits.append(self.journal)
        if self.year:
            bits.append(str(self.year))
        return ", ".join(b for b in bits if b)


class KeyWork(BaseModel):
    title: str = Field(..., description="Title of a retrieved work that is central to the topic.")
    authors: str = Field("", description="Author(s), as a short string.")
    why: str = Field(..., description="One sentence: why this work matters for the topic.")


class SourceJudgement(BaseModel):
    """The model's quality verdict on one retrieved bibliography entry.

    Lets downstream features (draft, review) refuse to dress speculative grey
    literature as scholarly convergence — the single concrete blocker the
    philosopher's trial named (REPORT.md §1.8 / §2.1)."""

    external_id: str = Field(
        ...,
        description="PhilArchive record id of the work being judged (must match a retrieved BiblioEntry).",
    )
    tier: SourceTier = Field(
        ...,
        description=(
            "canonical: a field-standard reference; peer_reviewed: published in a "
            "recognised venue; speculative: preprint, self-published, or fringe; "
            "off_topic: retrieved but not actually about the topic."
        ),
    )
    rationale: str = Field(
        ...,
        description="One sentence: why this tier (venue, author standing, peer-review status, topical fit).",
    )
    cite_as: CiteAs = Field(
        ...,
        description=(
            "primary: lead the argument with this; supporting: cite alongside primary; "
            "background: cite once for context; do_not_cite: ignore in the draft."
        ),
    )


class MissingCanonical(BaseModel):
    """A canonical author / literature the corpus *should* have surfaced but did not.

    Surfaced as ``[citation needed]`` cues in the draft so the philosopher
    sees the gap without it being silently papered over."""

    author: str = Field(..., description="The canonical author or research programme that is missing.")
    work_hint: str = Field(
        "",
        description="Optional pointer to a specific line of work (e.g. 'transience-of-memory papers').",
    )
    why: str = Field(..., description="One sentence: why this absence matters for the topic.")


class TopicSynthesis(BaseModel):
    """Claude's structured reading of the literature on a topic."""

    overview: str = Field(
        ...,
        description="2–4 paragraphs summarizing what is discussed on the topic in the retrieved literature.",
    )
    big_questions: list[str] = Field(
        default_factory=list,
        description="The major, foundational questions/debates the topic turns on.",
    )
    small_questions: list[str] = Field(
        default_factory=list,
        description="More specific, technical, or downstream sub-questions.",
    )
    key_works: list[KeyWork] = Field(default_factory=list)
    suggested_searches: list[str] = Field(
        default_factory=list,
        description="Refined follow-up search queries to deepen the bibliography.",
    )
    source_judgements: list[SourceJudgement] = Field(
        default_factory=list,
        description=(
            "One verdict per retrieved entry: tier (canonical|peer_reviewed|"
            "speculative|off_topic) and how it should be cited. Downstream "
            "features (draft, review) consume this to avoid dressing grey "
            "literature as scholarly convergence."
        ),
    )
    missing_canonical: list[MissingCanonical] = Field(
        default_factory=list,
        description=(
            "Canonical authors / lines of work the topic clearly turns on but "
            "the retrieved corpus failed to surface. Carried into the draft "
            "as [citation needed] cues so the gap is visible, not hidden."
        ),
    )
    corpus_caveats: str = Field(
        "",
        description=(
            "One short paragraph: how thin or uneven the retrieved corpus is "
            "on the user's specific framing, and which best-matching items "
            "are heterodox/self-published rather than peer-reviewed."
        ),
    )
