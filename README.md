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

## Bibliography (PhilArchive)

Search the open-access PhilArchive index — no API key needed:

```bash
cophilo biblio search "moral luck" --limit 20        # human-readable
cophilo biblio search "moral luck" --json            # for tooling / Claude Code
```

Hits are upserted (idempotently) into the `bibliography` table unless
`--no-save` is passed.

Have Claude read a topic's literature and synthesize it — the overview, the
big questions, and the smaller ones. By default this runs through the local
**Claude Code CLI** (no API key); set `COPHILO_LLM_BACKEND=api` with
`ANTHROPIC_API_KEY` to use the Anthropic SDK instead:

```bash
cophilo biblio synthesize --topic "Whether the ability to do otherwise
  survives determinism, focusing on modal accounts." --out synthesis.md
# or:  --topic-file topic.txt   |   echo "<topic>" | cophilo biblio synthesize
# use --query to set a focused PhilArchive query distinct from the topic prose
```

`data/journals.yaml` is a trusted list of English-language philosophy
journals (scope, typical length, OA status) used by the bibliography scorer.

## Status

M1 (ingest), M2 (extraction), and the M7 bibliography slice
(PhilArchive search + topic synthesis) are in place.
