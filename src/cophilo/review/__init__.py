"""Critical review.

``review_file`` reads a text file (.tex, .md, …), asks Claude for an honest
line-anchored critique, and writes the remarks back into the file as marked,
non-rendering comments that can be cleared with ``clear_review_comments``.
"""

from cophilo.review.runner import (
    MAX_ROUND_DEPTH,
    CounterResult,
    ReviewResult,
    clear_review_comments,
    respond_to_review,
    review_file,
    round_path,
    sidecar_path,
)

__all__ = [
    "ReviewResult",
    "CounterResult",
    "MAX_ROUND_DEPTH",
    "review_file",
    "respond_to_review",
    "clear_review_comments",
    "round_path",
    "sidecar_path",
]
