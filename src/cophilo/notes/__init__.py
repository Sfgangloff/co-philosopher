"""Note capture: an offline, verbatim REPL that writes Markdown notes into
``data/corpus/notes/`` for later ingest/extraction.
"""

from cophilo.notes.capture import DialogSession, run_dialog

__all__ = ["DialogSession", "run_dialog"]
