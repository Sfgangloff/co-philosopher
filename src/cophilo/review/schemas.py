"""Typed shapes for the critical-review pass.

``FileReview`` mirrors the JSON Claude returns from the review prompt and is
validated by the same ``messages.parse()`` / CLI-JSON path as the extraction,
synthesis, and draft passes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CommentKind = Literal["strength", "weakness", "question", "suggestion", "clarity"]


class ReviewComment(BaseModel):
    """One line-anchored remark from the critical reviewer."""

    line: int = Field(
        ...,
        description=(
            "1-indexed line number, in the numbered source you were given, "
            "that this remark is anchored to. Use 0 for a general remark not "
            "tied to a specific line."
        ),
    )
    kind: CommentKind = Field(
        ...,
        description="strength | weakness | question | suggestion | clarity",
    )
    comment: str = Field(
        ...,
        description=(
            "One specific, honest remark about that line — a few sentences at "
            "most. Critical where criticism is earned; say plainly when "
            "something is well done."
        ),
    )
    anchor: str = Field(
        default="",
        description=(
            "A short verbatim excerpt (roughly 4–12 words) copied EXACTLY "
            "from the line this remark is about — no line numbers, no "
            "ellipses, no paraphrase. Used to re-locate the remark if the "
            "document is edited before a re-review. Leave empty only for a "
            "general (line 0) remark."
        ),
    )


CoverageStatus = Literal["engaged", "partial", "skipped"]
CounterVerdict = Literal["concede", "sharpen", "pivot"]
ClaimVerdict = Literal[
    "supported", "unsupported", "overclaim", "contradicted", "missing_citation"
]


class CounterReply(BaseModel):
    """One round of the marginalia-as-conversation move (REPORT.md §2.2).

    Given (original critique → user reply), the model produces a
    counter-reply that either concedes the user's defense, sharpens the
    worry into a stronger version, or pivots to a related-but-different
    objection. Closes the gap the philosopher named — that the review pass
    today is a "brilliant one-shot referee but still a monologue."
    """

    original_comment: str = Field(
        ..., description="The prior round's critique, verbatim."
    )
    user_reply: str = Field(
        ..., description="The user's reply to that critique, verbatim."
    )
    counter: str = Field(
        ...,
        description=(
            "The model's second-round response: one short paragraph. Do not "
            "repeat the original critique. Engage the user's reply directly."
        ),
    )
    verdict: CounterVerdict = Field(
        ...,
        description=(
            "concede: the reply is right; sharpen: the worry survives in a "
            "stronger form; pivot: a related-but-different objection now bites."
        ),
    )
    anchor: str = Field(
        "",
        description=(
            "Short verbatim excerpt (≈4–12 words) from the original critique, "
            "so the counter can be re-anchored if the prior review is edited."
        ),
    )


class CounterRound(BaseModel):
    """One round of counter-replies — what `review --respond-to` returns."""

    summary: str = Field(
        ...,
        description=(
            "2–3 sentences: how the dialectic stands after this round. Which "
            "worries have been conceded, which survive, which have shifted."
        ),
    )
    counters: list[CounterReply] = Field(default_factory=list)


class QuestionCoverage(BaseModel):
    """Did the draft engage one of the open questions ``propose`` pre-flagged?

    Closes the §2.2 loop: ``propose``'s open questions and ``review``'s
    weaknesses were "the same worries said twice"; the model now reports the
    overlap explicitly."""

    question: str = Field(..., description="The open question, verbatim from OUTLINE.md.")
    status: CoverageStatus = Field(
        ...,
        description="engaged: the draft addresses it; partial: gestures at it; skipped: ignored.",
    )
    evidence: str = Field(
        "",
        description="One short sentence pointing to where (or where not) in the draft.",
    )
    evidence_line: int = Field(
        0,
        description="1-indexed line that best supports the verdict; 0 if there is no anchor.",
    )


class ClaimAssessment(BaseModel):
    """One bibliography-aware verdict on a claim the draft makes (or omits).

    Closes the §3 loop the philosopher's trial named: at draft-time the
    bibliography is judged for tier and cite_as; at review-time the *draft's
    claims* are checked against that same judgement. Caught at review, not
    only at post-publication referee."""

    claim: str = Field(
        ...,
        description=(
            "A short paraphrase of the claim being assessed, in the reviewer's "
            "words. For 'missing_citation' verdicts, the claim is the one the "
            "draft makes that needs evidence; for 'unsupported'/'contradicted', "
            "the claim is what the draft asserts that the bibliography does not "
            "(or does the opposite of) support."
        ),
    )
    verdict: ClaimVerdict = Field(
        ...,
        description=(
            "supported: a canonical/peer-reviewed entry in the bibliography "
            "backs this claim; unsupported: no entry in the bibliography backs "
            "it (and the missing-canonical list flags a likely gap); "
            "contradicted: the bibliography says the opposite; overclaim: "
            "supported in a weaker form than the draft asserts; "
            "missing_citation: the claim needs a citation and the draft offers "
            "none (typically paired with cite_suggestions)."
        ),
    )
    evidence: str = Field(
        ...,
        description=(
            "One short sentence naming the work(s) — by author/year or "
            "external_id from the bibliography — that the verdict rests on, or "
            "the missing-canonical entry that flags the gap."
        ),
    )
    evidence_line: int = Field(
        0,
        description=(
            "1-indexed line in the draft where this claim is made (0 if it "
            "spans multiple places or is a missing-claim verdict with no anchor)."
        ),
    )
    cite_suggestions: list[str] = Field(
        default_factory=list,
        description=(
            "PhilArchive external_ids from the bibliography that the draft "
            "should cite for this claim (or whose absence the draft should "
            "acknowledge). Use the exact ids shown in the bibliography listing."
        ),
    )


class FileReview(BaseModel):
    """Claude's critical-but-honest reading of a single file."""

    summary: str = Field(
        ...,
        description=(
            "An honest overall assessment in 2–4 sentences: what the piece is "
            "trying to do, what genuinely works, and the most important "
            "problems to address."
        ),
    )
    comments: list[ReviewComment] = Field(
        default_factory=list,
        description=(
            "Line-anchored remarks in ascending line order. Be selective: "
            "comment where it matters, not on every line."
        ),
    )
    propose_question_coverage: list[QuestionCoverage] = Field(
        default_factory=list,
        description=(
            "One verdict per question that `propose` flagged at draft-creation "
            "time (when OUTLINE.md was available). Empty when no propose-time "
            "questions were passed to the reviewer."
        ),
    )
    bibliography_check: list[ClaimAssessment] = Field(
        default_factory=list,
        description=(
            "Bibliography-aware verdicts on the draft's central claims. "
            "Populated only when the reviewer was given a saved synthesis "
            "(`cophilo review --against <synthesis.json>`); empty otherwise."
        ),
    )
