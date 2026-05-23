# co-philosopher

An interactive philosophy assistant that lives in your terminal. It ingests
your articles and notes, extracts the concepts and questions you work with,
stores them in a queryable SQLite database, helps you turn a coherent cluster
of notes into an article draft, and curates bibliography from PhilArchive.

Key-free by default (it drives the local **Claude Code CLI**; no API key),
offline-first where it can be, and git-friendly. See [`PLAN.md`](PLAN.md) for
the full design and milestones.

## Quick start

After cloning your fork, it's two steps:

```bash
./setup.sh        # 1. venv + dependencies + `cophilo init` (idempotent)
./cophilo         # 2. start the terminal interface — type `help` inside
```

`setup.sh` uses [`uv`](https://docs.astral.sh/uv/) if present (much faster),
otherwise falls back to `python3 -m venv` + `pip`. `./cophilo` is a thin
wrapper so you don't have to activate the venv; equivalently
`source .venv/bin/activate && cophilo`. `make setup` / `make test` /
`make lint` are also wired up. Want the offline journals memory index too?
`COPHILO_EXTRAS="dev,memory" ./setup.sh`.

Pandoc is required for DOCX/LaTeX ingestion (`brew install pandoc` on macOS);
the GitHub CLI [`gh`](https://cli.github.com) is needed for `cophilo backup`
(or use `--remote`). `setup.sh` warns if either is missing. Copy
`.env.example` to `.env` only to override defaults (e.g.
`COPHILO_LLM_BACKEND=api` with `ANTHROPIC_API_KEY` to use the Anthropic SDK
instead of the local Claude Code CLI).

## Home screen

Running `cophilo` with no arguments opens a Claude-Code-style splash — a
title bar, a discretised philosopher portrait, and a prompt. Type `help`
(or run `cophilo help`) for the full command list with every option and its
description, introspected from the CLI so it never drifts (the `backup`
command is highlighted in purple so the safety net stands out). Off a TTY it
just prints the splash and exits.

## The corpus

Everything you feed in lives under `data/corpus/`, in three subfolders:

| Folder | Holds | Ingested as |
|---|---|---|
| `notes/` | raw notes (e.g. from `cophilo dialog`) | `note` |
| `articles/` | papers you've already written | `article` |
| `drafts/` | papers we write together (`propose`/`draft`) | *skipped* |

The corpus — and everything built from it (`data/normalized/`,
`data/rendered/`, `data/proposals/`, and the extraction DB
`data/db/cophilo.sqlite`) — is **personal** and **gitignored** by this
tool repo. None of it is lost: `cophilo backup` keeps all of it under
version control in a separate private repository (see below). The only
`data/` exception tracked here is `journals.yaml`, a curated source list.

## Notes → draft workflow

```bash
cophilo dialog --topic "free will"   # offline REPL; write a note over many
                                     #   lines, a BLANK LINE commits it as one
                                     #   coherent note (not one file per line)
cophilo ingest                       # no path → ingest data/corpus: kind is
                                     #   inferred per subfolder, drafts/ is
                                     #   skipped, only new files are touched
cophilo extract                      # Claude pulls concepts + questions;
                                     #   already-extracted docs are skipped
                                     #   (--force to re-run); new concepts
                                     #   go to a review queue
cophilo concepts                     # see what extract found (confirmed +
cophilo questions                    #   queued proposals; --doc N, --json)
cophilo propose                      # Claude finds a coherent article in your
                                     #   notes; on accept it creates
                                     #   drafts/<slug>/ and MOVES those notes in
cophilo draft drafts/<slug>          # pull a PhilArchive bibliography from the
                                     #   thesis and draft article.tex in place
```

`dialog` is fully offline (no LLM, no network). `propose`/`draft`/`extract`
use the configured Claude backend (the local CLI by default — no API key).
PDF / DOCX / LaTeX / Markdown are all ingestable.

## Critical review

Have Claude read one file and write an honest, line-by-line critique **into
the file itself** — unsupported premises, equivocal terms, gaps, missed
objections, and (sparingly) what genuinely works:

```bash
cophilo review drafts/free-will/article.tex   # writes comments in place
cophilo review notes/sketch.md --dry-run       # print annotated, don't write
cophilo review article.tex --clear             # remove the comments (no LLM)
cophilo review paper.tex --format sidecar      # write paper.tex.review.md;
                                               #   never touch the source
cophilo review paper.tex --only weakness,question  # keep just those kinds
```

Comments are inserted on their own lines, immediately before the line they
discuss, in the file's native comment syntax — `% …` for LaTeX, an invisible
`<!-- … -->` for Markdown/HTML, `# …` for scripts, a loud `>>> …` marker for
plain text — so the document still compiles/renders unchanged and the remarks
never look like prose. Every line carries a `cophilo-review` sentinel, which
makes the pass **reversible** (`--clear`) and **idempotent**: re-running
strips the previous review before writing a fresh one, and your original
lines are never modified. Each comment also carries a short verbatim
**anchor** quote, so if you edit the file and re-review, a remark re-finds
its line instead of landing on whatever now sits at the old line number.
`--format sidecar` keeps every mark out of the source (a shared `.tex`);
`--only <kinds>` filters the pass. The review language is auto-detected
(override with `--lang en|fr`). Uses the configured Claude backend (local
CLI by default).

## Backup (private)

`data/` is kept as its own independent git repository whose working tree
tracks exactly the personal/derived paths (`corpus/`, `normalized/`,
`rendered/`, `proposals/`, `db/cophilo.sqlite`) and pushed to a **private**
backup repo. The journals memory index and `journals.yaml` are excluded
(they derive from a source already tracked in the main repo). Nothing is
hard-coded, so this works on anyone's fork:

```bash
cophilo backup            # ensure <your-gh-user>/co-philosopher-backup exists
                          #   (private; created on first run), commit + push
cophilo backup --name my-corpus-backup
cophilo backup --remote git@example.org:me/mirror.git   # skip gh entirely
```

It resolves your GitHub account from the authenticated [`gh`](https://cli.github.com)
CLI (`gh auth login` once), creates the private repo if missing, initialises
`data/` as a git repo on first run, then commits and pushes the backed-up
paths. Use `--remote` to push to any git URL (self-host, GitLab, a bare repo)
without `gh`.

## Bibliography (PhilArchive)

Search the open-access PhilArchive index — no API key needed:

```bash
cophilo biblio search "moral luck" --limit 20        # human-readable
cophilo biblio search "moral luck" --json            # for tooling / Claude Code
```

Hits are upserted (idempotently) into the `bibliography` table unless
`--no-save` is passed.

Have Claude read a topic's literature and synthesize it — the overview, the
big questions, and the smaller ones:

```bash
cophilo biblio synthesize --topic "Whether the ability to do otherwise
  survives determinism, focusing on modal accounts." --out synthesis.md
# or:  --topic-file topic.txt   |   echo "<topic>" | cophilo biblio synthesize
# use --query to set a focused PhilArchive query distinct from the topic prose
```

`data/journals.yaml` is a trusted list of English-language philosophy
journals (scope, typical length, OA status) used by the bibliography scorer.

## Semantic memory (MCP)

The journals catalog is also exposed to Claude Code as a local vector
search tool. Embeddings are computed offline with `fastembed`
(ONNX, no API key; the model downloads once on first use) and stored in a
`sqlite-vec` database at `data/db/memory.sqlite` — a derived artifact,
rebuilt from `data/journals.yaml` and gitignored like the other DBs.

```bash
uv pip install -e ".[memory]"                    # sqlite-vec + fastembed + mcp
cophilo memory index                             # build/refresh the store
cophilo memory search "modal accounts of free will" -n 5
cophilo memory search "phenomenology of perception" --oa --json
```

The store self-heals: editing `journals.yaml` or changing
`COPHILO_MEMORY_EMBEDDING_MODEL` triggers a transparent rebuild on the
next query.

`.mcp.json` registers a project-scoped MCP server (`cophilo-memory`)
exposing `search_journals` and `get_journal` tools. After
`uv pip install -e ".[memory]"`, restart Claude Code (or run `/mcp`) so it
picks the server up; relative `.venv/bin/cophilo-memory-mcp` resolves
from the repo root.

## Running Claude Code on this repo

This repo ships rails for autonomous Claude Code sessions: `.claude/CLAUDE.md`
+ `.claude/settings.json` (allow/deny + hook bindings) + `.claude/hooks/*.sh`
(runtime guards) + `.githooks/*` (git-side belt-and-braces). The rails block
writes to private `data/`, refuse force-push / `--no-verify` / cost-bearing
`cophilo` subcommands without explicit owner approval, and run ruff + pytest
before every commit.

One-time per clone — point git at the tracked hooks:

```bash
git config core.hooksPath .githooks
```

Launch Claude Code with the project rails loaded:

```bash
./bin/cc                  # interactive; cwd-scoped; strict MCP config
./bin/cc "<prompt>"       # one-shot
```

`bin/cc` runs `claude --permission-mode acceptEdits --strict-mcp-config`
from the repo root. Edits are auto-approved (the `pre-write.sh` hook
polices their targets); Bash prompts unless allowlisted. To authorise
push for **one session**: `COPHILO_ALLOW_PUSH=1 ./bin/cc`.

Per-user settings (anything not shared) belong in `.claude/settings.local.json`
(gitignored).

## Status

In place: one-step `setup.sh`, M1 (ingest, incl. Markdown), M2 (extraction),
the M7 bibliography slice (PhilArchive search + topic synthesis), a local
semantic-memory MCP server, the notes → draft workflow (`dialog` /
corpus-default `ingest` / `propose` / `draft`), critical `review` of a single
file, the `cophilo` home screen + `help`, and private `backup` of the corpus
and everything derived from it.
