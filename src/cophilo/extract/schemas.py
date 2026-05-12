"""Pydantic models for Claude's structured output.

These mirror the JSON shapes our prompts ask Claude to return. Claude's
``messages.parse()`` validates the response against these models, so a
malformed reply raises rather than silently writing junk to the DB.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ConceptRole = Literal["introduce", "define", "use", "critique", "cite"]
QuestionRole = Literal["raise", "reformulate", "attempt", "answer"]


class ConceptMention(BaseModel):
    """One mention of a concept in a single passage."""

    passage_ord: int = Field(..., description="The 1-based ord of the passage this belongs to.")
    slug: str | None = Field(
        None,
        description="If the concept already exists in the taxonomy, its slug. Null for new concepts.",
    )
    is_new: bool = Field(..., description="True iff this concept is not in the existing taxonomy.")
    proposed_canonical_label_en: str | None = Field(
        None, description="Required when is_new=True. English label for the new concept."
    )
    proposed_canonical_label_fr: str | None = Field(
        None, description="Required when is_new=True. French label for the new concept."
    )
    proposed_description: str | None = Field(
        None,
        description="Required when is_new=True. One-paragraph description of the new concept.",
    )
    role: ConceptRole = Field(..., description="How this passage uses the concept.")
    span_quote: str = Field(..., description="A short verbatim quote from the passage.")
    confidence: float = Field(..., ge=0.0, le=1.0)
    attributed_authors: list[str] = Field(
        default_factory=list,
        description="External authors associated with this concept in this passage (e.g. 'Husserl').",
    )


class ConceptPassResponse(BaseModel):
    mentions: list[ConceptMention]
    notes: str | None = Field(
        None,
        description="Optional free-form notes from Claude (e.g. flagged passages with no fitting concept).",
    )


class QuestionMention(BaseModel):
    passage_ord: int
    label: str = Field(..., description="A short label for the question (under 12 words).")
    description: str = Field(..., description="One-sentence statement of the question.")
    role: QuestionRole
    explicit: bool = Field(..., description="True if the question is stated; false if implied.")
    span_quote: str
    confidence: float = Field(..., ge=0.0, le=1.0)


class QuestionPassResponse(BaseModel):
    questions: list[QuestionMention]
