# Co-philosopher — implementation plan

A CLI tool for interactive philosophy: ingest your articles and notes, extract
the concepts and questions you address across them, store them in a queryable
database, render annotated text, and use the resulting taxonomy to categorize
new notes, propose articles, and curate bibliography.

Inputs: PDF / DOCX / LaTeX. Languages: EN + FR.
LLM: Anthropic API (Claude). Embeddings: OpenAI `text-embedding-3-large`.
Output: Markdown source-of-truth + generated HTML view.
Taxonomy changes: human-in-the-loop review queue.
Bibliography: PhilPapers.

## 1. Architecture

A Python CLI (`cophilo`) ingests source files into normalized Markdown, runs a
multi-pass Claude extraction over each document to populate a SQLite database
of **concepts, questions, claims, mentions, and relations**, and writes
annotated Markdown + a generated HTML view back to disk.

A separate **notes** pipeline categorizes lightweight notes against the
existing taxonomy and surfaces new clusters. A **review queue** is the only
place new categories get created or merged — every taxonomy change passes
under your eyes. PhilPapers powers a curated bibliography for any concept,
article draft, or note cluster.

Everything lives in one git repo: source files, normalized Markdown,
annotations, and the SQLite DB. Diff-friendly, no server.

## 2. Repo layout

```
co-philosopher/
  pyproject.toml
  PLAN.md
  README.md
  src/cophilo/
    cli.py                  # Typer entrypoint
    config.py               # paths, API keys, language defaults
    db/
      schema.sql
      migrations/
      models.py             # thin sqlite3 wrappers
    ingest/
      pdf.py                # PyMuPDF + layout heuristics
      docx.py               # pandoc → markdown
      tex.py                # pandoc + macro/citation handling
      normalize.py          # canonical markdown + frontmatter
    extract/
      claude.py             # Anthropic SDK wrapper, prompt caching
      passes.py             # concepts, questions, claims, attributions, spans
      prompts/{en,fr}/      # one file per pass per language
    taxonomy/
      embed.py              # OpenAI embeddings
      cluster.py            # periodic re-clustering, drift detection
      review_queue.py       # propose/merge/rename/reject ops
    annotate/
      tagger.py             # map concept mentions → exact spans
      render_md.py          # canonical annotated markdown
      render_html.py        # margin-annotation HTML view
    query/
      search.py             # semantic + keyword
      concept.py            # show definition, mentions, evolution
      tensions.py           # contradiction/tension detector
    notes/
      capture.py            # quick capture
      categorize.py         # assign to existing concepts
      trends.py             # detect emerging clusters
    propose/
      article.py            # outline from note clusters
    biblio/
      philpapers.py         # API client
      curate.py             # rank, filter, link to concepts
    review/
      cli.py                # interactive review queue
  data/
    corpus/                 # source files (read-only originals)
    normalized/             # cleaned markdown per document
    rendered/               # annotated md + html outputs
    db/cophilo.sqlite
  tests/
```

## 3. Data model (SQLite)

```sql
CREATE TABLE documents (
  id INTEGER PRIMARY KEY,
  kind TEXT CHECK(kind IN ('article','note')),
  title TEXT,
  source_path TEXT UNIQUE,
  language TEXT,                    -- 'en' | 'fr'
  status TEXT,                      -- ingested|extracted|annotated
  ingested_at TEXT,
  metadata_json TEXT
);

CREATE TABLE passages (
  id INTEGER PRIMARY KEY,
  document_id INTEGER REFERENCES documents(id),
  ord INTEGER,
  char_start INTEGER, char_end INTEGER,
  section_path TEXT,
  text TEXT
);

CREATE TABLE concepts (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE,
  canonical_label_en TEXT,
  canonical_label_fr TEXT,
  description TEXT,
  kind TEXT CHECK(kind IN ('mine','created','external')),
  status TEXT CHECK(status IN ('draft','confirmed','merged')),
  merged_into INTEGER REFERENCES concepts(id),
  created_at TEXT, confirmed_at TEXT
);

CREATE TABLE concept_aliases (
  concept_id INTEGER REFERENCES concepts(id),
  alias TEXT, language TEXT
);

CREATE TABLE concept_mentions (
  id INTEGER PRIMARY KEY,
  concept_id INTEGER REFERENCES concepts(id),
  passage_id INTEGER REFERENCES passages(id),
  span_start INTEGER, span_end INTEGER,
  role TEXT,                        -- introduce|define|use|critique|cite
  confidence REAL
);

CREATE TABLE questions (
  id INTEGER PRIMARY KEY,
  label TEXT, description TEXT,
  status TEXT CHECK(status IN ('open','answered','dormant')),
  first_raised_passage_id INTEGER REFERENCES passages(id)
);

CREATE TABLE question_mentions (
  question_id INTEGER REFERENCES questions(id),
  passage_id INTEGER REFERENCES passages(id),
  role TEXT
);

CREATE TABLE claims (
  id INTEGER PRIMARY KEY,
  passage_id INTEGER REFERENCES passages(id),
  statement TEXT,
  polarity TEXT,
  metadata_json TEXT
);

CREATE TABLE claim_concepts (claim_id INTEGER, concept_id INTEGER);

CREATE TABLE relations (
  src_concept_id INTEGER, dst_concept_id INTEGER,
  kind TEXT,
  weight REAL, source_passage_id INTEGER
);

CREATE TABLE external_authors (
  id INTEGER PRIMARY KEY,
  name TEXT, normalized_name TEXT UNIQUE
);

CREATE TABLE concept_external_authors (concept_id INTEGER, author_id INTEGER);

CREATE TABLE embeddings (
  ref_kind TEXT, ref_id INTEGER,
  vector BLOB,
  model TEXT
);

CREATE TABLE review_queue (
  id INTEGER PRIMARY KEY,
  kind TEXT,                        -- new_concept|merge|relabel|drift|unfit_passage
  payload_json TEXT,
  status TEXT CHECK(status IN ('pending','accepted','rejected','deferred')),
  created_at TEXT, decided_at TEXT, notes TEXT
);

CREATE TABLE bibliography (
  id INTEGER PRIMARY KEY,
  source TEXT, external_id TEXT,
  title TEXT, authors TEXT, journal TEXT, year INTEGER,
  abstract TEXT, doi TEXT,
  quality_score REAL, fetched_at TEXT
);

CREATE TABLE bibliography_links (
  bibliography_id INTEGER,
  target_kind TEXT, target_id INTEGER,
  rationale TEXT
);
```

`sqlite-vec` provides vector search inside the same DB file.

## 4. Ingest pipeline

For each input format → produce **normalized Markdown** with YAML frontmatter
(`title`, `language`, `source`, detected `year`, `author`).

- **PDF**: PyMuPDF + pdfplumber fallback; section detection from font-size
  heuristics; footnotes pulled separately; page numbers preserved as HTML
  comments for round-trip.
- **DOCX**: `pandoc -f docx -t markdown --wrap=none --extract-media=...`.
- **LaTeX**: `pandoc -f latex -t markdown` with a small pre-pass that expands
  your common macros; `\concept{X}` macros (if used) harvested as a hint.

Output: `data/normalized/<doc-id>.md`. The DB stores the path.

**Language detection**: `lingua-py` per document (handles short EN/FR
reliably). Stored on `documents.language`; drives prompt selection.

## 5. Extraction (multi-pass, Claude)

Each pass is a separate prompt. Passes run sequentially per document with
**prompt caching** on stable parts (current taxonomy snapshot + style guide).
**Sonnet 4.6** for routine passes; **Opus 4.7** only for taxonomy decisions.

1. **Structural pass** — section tree, footnotes, citations (deterministic).
2. **Concept pass** — JSON list of concept references with
   `{slug_or_new_label, role, span_quote, confidence, evidence}`. New labels
   are flagged for the review queue, not auto-created.
3. **Question pass** — open questions: explicit and implicit.
4. **Claim pass** — atomic claims with polarity and concepts involved. Drives
   later tension detection.
5. **Attribution pass** — for each external author mentioned, which concepts
   are linked to them; for `created` candidates, ask Claude for a
   one-paragraph "novelty defense" (you confirm in review).
6. **Span pass** — back-resolve every accepted mention to exact character
   offsets in the normalized markdown via fuzzy matching against `span_quote`.

## 6. Taxonomy & human-in-the-loop

- **Embeddings**: every concept (label + description + 3 representative
  mentions) and every passage gets a `text-embedding-3-large` vector.
  Multilingual.
- **New concept proposal**: extraction emits a candidate → enqueued in
  `review_queue` with kind `new_concept`. Review CLI shows: proposed label
  (EN+FR), draft definition, 3 example passages, top-5 nearest existing
  concepts. You: **accept / merge / rename / reject / defer**.
- **Drift detection** (`cophilo taxonomy recluster`): re-cluster all concept
  embeddings; flag close pairs as `merge` items, spread concepts as `split`
  items.
- **Unfit passages**: `concept: <none>` for important-but-unmatched passages.
  Queued as `unfit_passage`.
- **`mine` vs `created` vs `external`**: defaults to `mine` for any concept
  first seen in your writing without a cited author; `external` if attributed
  to a named author; `created` only on explicit promotion in review (with the
  LLM's novelty defense shown).

## 7. Annotation rendering

- **Canonical Markdown** at `data/rendered/<doc>.annotated.md`:

  ```
  Some text discussing [[c:free-will|free will]]{role=define}, then later
  the question of [[q:free-will-determinism]]{role=raise} arises.
  ```

  Round-trippable, diffable in git, plain-text searchable. Sidecar
  `<doc>.spans.json` carries machine-readable offsets.
- **HTML view** at `data/rendered/<doc>.html`: generated by
  `cophilo render <doc> --html`. Spans colored by category with right-margin
  labels and concept tooltips. Self-contained file.

## 8. CLI surface

```
cophilo                                 # home screen (splash) + offline prompt
cophilo help                            # every command, with options + descriptions
cophilo init                            # set up DB, config, .env
cophilo backup [--name N] [--remote URL] # push corpus → private backup repo
cophilo dialog [--topic T] [--lang]     # offline note-capture REPL → corpus/notes
cophilo ingest [path]                   # default: corpus/ (kind per subfolder,
                                        #   drafts/ skipped, only what's new)
cophilo extract [--doc ID] [--pass NAME] [--all]
cophilo review                          # interactive queue
cophilo render <doc> [--html]
cophilo search "<query>" [--lang en|fr] [--kind concept|passage]
cophilo concept <slug>
cophilo concept evolve <slug>           # paragraph-form summary across years
cophilo question <id>
cophilo tensions [--concept SLUG]
cophilo notes categorize [--since DATE]
cophilo trends [--window 30d]
cophilo propose                         # find a coherent article in your notes;
                                        #   on accept → drafts/<slug>/ + move notes
cophilo draft <drafts/slug>             # biblio + Claude → article.tex in the folder
cophilo biblio <concept-slug | --draft PATH>
cophilo taxonomy recluster
cophilo export concept-graph [--out FILE.svg]
```

## 9. Notes pipeline

- **Capture**: `cophilo dialog` runs an offline, verbatim REPL — each line is
  written as Markdown (with frontmatter) into `data/corpus/notes/`. No LLM, no
  network. Picked up by the next `cophilo ingest` like any corpus file.
- **Categorize**: concept pass only, against existing taxonomy. New-concept
  candidates from notes still go to the review queue.
- **Trends**: cluster notes from the last N days by embedding; clusters of
  ≥3 notes that don't strongly map to existing concepts surface as
  candidate emerging themes.

## 10. Article proposals

`cophilo propose article` works in two modes:
- `--topic <slug>`: gather all notes + passages mentioning the concept,
  cluster sub-themes, ask Claude to draft an article outline (sections,
  theses, open questions) plus a candidate **target journal** with a
  one-line fit rationale (drawn from PhilPapers metadata of similar work).
- no flag: scan recent note trends, propose 3 candidate articles ranked by
  cluster size and concept centrality.

Output: `data/proposals/<slug>.md` with sections, draft theses, citations to
your own passages (linked by document+span), open questions still to resolve,
and a starter bibliography.

## 11. Bibliography (PhilPapers)

`cophilo biblio <concept-slug>` or `cophilo biblio --draft <path>`:
1. Build a query from concept labels (EN+FR) + 2–3 nearest concepts.
2. Hit PhilPapers API; pull title, authors, journal, year, abstract.
3. **Quality score** = weighted combination of: journal in trusted list
   (`data/journals.yaml`), citation count if available, recency, abstract
   semantic similarity to concept description.
4. Top N (default 10) with a one-sentence "why this paper" rationale
   generated by Claude using the abstract + concept description.
5. Saved into `bibliography_links` to avoid re-fetching.

## 12. Phased delivery

| Milestone | Scope |
|---|---|
| **M1 — Foundation** | Repo, config, DB schema, ingest for PDF/docx/tex → normalized markdown, language detection |
| **M2 — Extraction v1** | Concept + question pass with Claude (EN+FR prompts), prompt caching, JSON validation; basic `search` and `concept` commands |
| **M3 — Taxonomy + HITL** | Embeddings, review queue CLI, merge/rename/reject ops, drift recluster |
| **M4 — Annotation rendering** | Markdown + HTML output, span back-resolution |
| **M5 — Notes** | Capture, categorize, trends |
| **M6 — Article proposals** | Outline-from-clusters, journal suggestion |
| **M7 — Bibliography** | PhilPapers client, scoring, "why this paper" rationale |
| **M8 — Advanced** | Tension detector, concept evolution timeline, Socratic prompts, concept-graph export |

## 13. Key design decisions

- **SQLite + sqlite-vec** rather than Postgres + pgvector: single file,
  git-friendly, no server.
- **Markdown as source of truth** for annotations: HTML is regenerated.
  Annotations survive in plain text under git.
- **Multi-pass extraction with prompt caching** rather than one mega-prompt:
  cheaper, more targeted re-runs after taxonomy changes.
- **No auto-creation of concepts**: review queue is the only door. Prevents
  taxonomy chaos.
- **EN/FR symmetric**: concepts carry both labels; embeddings are
  multilingual; prompts are language-matched per document.
- **Claude Code as collaborator, not core**: program runs standalone via API.
  Claude Code is used for ad-hoc operations on the data.
