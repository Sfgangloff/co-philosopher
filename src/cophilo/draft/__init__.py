"""Draft pipeline.

``propose`` scans the user's notes and asks Claude whether a coherent
article hides in a subset of them; on acceptance the notes are moved into a
``data/corpus/drafts/<slug>/`` folder. ``compose`` then turns one such draft
folder (its notes + outline) plus a freshly retrieved bibliography into an
``article.tex``.
"""

from cophilo.draft.compose import ComposeResult, compose_draft
from cophilo.draft.propose import ProposeResult, accept_proposal, propose_articles

__all__ = [
    "ProposeResult",
    "propose_articles",
    "accept_proposal",
    "ComposeResult",
    "compose_draft",
]
