"""Typed shapes for the notes module.

``SocraticQuestion`` is what Claude returns when ``cophilo dialog
--socratic`` asks for *one* question back after a committed note. The
shape is deliberately minimal: a single field, so the model can't pad the
return with summaries or affirmations the philosopher's trial explicitly
ruled out (REPORT.md §3)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SocraticQuestion(BaseModel):
    """One sharp question back at a freshly committed note.

    Not a summary, not an affirmation, not a list — one question, the kind
    a sharp interlocutor would actually ask on hearing the note. The model
    is constrained to a single field so it cannot drift toward what the
    philosopher named the wrong move ("very tidy filing cabinet")."""

    question: str = Field(
        ...,
        description=(
            "A single question (max 2 sentences) that pushes the user's "
            "thought: name an unstated premise, an objection that bites, a "
            "distinction the note depends on, or a counter-case. Do not "
            "paraphrase the note. Do not begin with 'Have you considered…' "
            "filler. Ask the question a colleague would actually ask."
        ),
    )
