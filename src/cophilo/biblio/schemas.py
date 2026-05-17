"""Typed shapes for bibliography search results and topic synthesis.

`BiblioEntry` is the normalized record returned by the PhilArchive client.
The `Synthesis*` models mirror the JSON Claude returns from the synthesize
prompt and are validated by ``messages.parse()`` (same pattern as the
extraction passes).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
