"""Typed shapes for the draft pipeline.

The ``ArticleProposal*`` models mirror the JSON Claude returns from the
propose prompt; ``ArticleDraft`` mirrors the compose prompt. Both are
validated by the same ``messages.parse()`` / CLI-JSON path as the extraction
and synthesis passes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ArticleProposal(BaseModel):
    """One coherent article that could be written from a subset of notes."""

    slug: str = Field(
        ...,
        description="Short kebab-case placeholder name for the draft folder, e.g. 'free-will-and-luck'.",
    )
    title: str = Field(..., description="Working title for the proposed article.")
    thesis: str = Field(..., description="One- or two-sentence central thesis.")
    rationale: str = Field(
        ...,
        description="Why these particular notes cohere into a single article.",
    )
    note_ids: list[int] = Field(
        default_factory=list,
        description="The integer ids (the [note N] labels) of the notes this article would draw on.",
    )
    outline: list[str] = Field(
        default_factory=list,
        description="Tentative section headings, in order.",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Questions still unresolved by the notes that the article must address.",
    )


class ArticleProposals(BaseModel):
    """Claude's answer to 'is there an article in these notes?'."""

    proposals: list[ArticleProposal] = Field(
        default_factory=list,
        description="Zero or more coherent article opportunities. Empty if the notes don't yet cohere.",
    )


class DraftSection(BaseModel):
    heading: str = Field(..., description="Section heading.")
    body: str = Field(
        ...,
        description="The section's prose: connected paragraphs, plain text (no markdown headings).",
    )


class ArticleDraft(BaseModel):
    """Claude's draft of a philosophy article from notes + bibliography."""

    title: str
    abstract: str = Field(..., description="A single-paragraph abstract.")
    keywords: list[str] = Field(default_factory=list)
    sections: list[DraftSection] = Field(
        default_factory=list,
        description="The body of the article, in order (Introduction … Conclusion).",
    )
    references: list[str] = Field(
        default_factory=list,
        description="Formatted citation strings, grounded ONLY in the supplied bibliography.",
    )
