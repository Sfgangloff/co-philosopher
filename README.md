# co-philosopher

Interactive philosophy assistant. Ingests your articles and notes, extracts the
concepts and questions you address, stores them in a SQLite database, renders
annotated text, and uses the resulting taxonomy to categorize new notes,
propose articles, and curate bibliography.

See [`PLAN.md`](PLAN.md) for the design and milestones.

## Setup

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env  # then fill in API keys
cophilo init
```

Pandoc must be installed (`brew install pandoc` on macOS) for DOCX/LaTeX
ingestion.

## Status

M1 (ingest + DB skeleton) in progress.
