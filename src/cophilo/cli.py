"""Typer entrypoint for the cophilo CLI."""

from __future__ import annotations

import json
import shutil
import sys
import textwrap
from pathlib import Path

import typer

from cophilo.backup import DEFAULT_REPO_NAME, BackupError, backup_corpus
from cophilo.biblio import philarchive
from cophilo.biblio.schemas import BiblioEntry
from cophilo.biblio.synthesize import (
    render_markdown,
    save_synthesis,
    synthesize_topic,
)
from cophilo.config import ensure_dirs, get_config
from cophilo.db import models as db
from cophilo.draft import accept_proposal, compose_draft, propose_articles
from cophilo.extract.passes import extract_document
from cophilo.ingest.dispatch import SUPPORTED_SUFFIXES, ingest_tree
from cophilo.notes import run_dialog
from cophilo.review import (
    MAX_ROUND_DEPTH,
    clear_review_comments,
    respond_to_review,
    review_file,
    sidecar_path,
)

app = typer.Typer(
    add_completion=False,
    help=(
        "Co-philosopher: interactive philosophy assistant. "
        "Run `cophilo help` for the full, readable catalogue of commands, "
        "options, and descriptions (richer than this --help)."
    ),
)


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    """Bare `cophilo` opens the home screen; `cophilo help` lists everything."""
    if ctx.invoked_subcommand is None:
        from cophilo.tui import run_home

        run_home(app)


@app.command("help")
def help_() -> None:
    """List every command, with its options and their descriptions."""
    from cophilo.tui import print_help

    print_help(app)


@app.command()
def init() -> None:
    """Create data directories and initialize the SQLite database."""
    cfg = get_config()
    ensure_dirs(cfg)
    db.init_db(cfg)
    typer.echo(f"Initialized cophilo at {cfg.repo_root}")
    typer.echo(f"  data dir: {cfg.data_dir}")
    typer.echo(f"  database: {cfg.db_path}")


@app.command()
def backup(
    name: str = typer.Option(
        DEFAULT_REPO_NAME,
        "--name",
        envvar="COPHILO_BACKUP_REPO",
        help="Backup repo name (created under your GitHub account if missing)",
    ),
    private: bool = typer.Option(
        True, "--private/--public", help="Repo visibility (default: private)"
    ),
    remote: str = typer.Option(
        None,
        "--remote",
        help="Push to this git URL instead of using gh (self-host, GitLab, …)",
    ),
    message: str = typer.Option(None, "--message", "-m", help="Commit message"),
) -> None:
    """Back up data/corpus to a separate private git repo, creating it under
    your own GitHub account on first run. Fork-friendly: nothing hard-coded."""
    cfg = get_config()
    ensure_dirs(cfg)
    try:
        result = backup_corpus(
            cfg, name=name, private=private, remote=remote, message=message
        )
    except BackupError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(result.summary())


@app.command()
def ingest(
    path: Path = typer.Argument(
        None,
        exists=True,
        readable=True,
        help="File or directory to ingest (default: data/corpus, excluding drafts/)",
    ),
    kind: str = typer.Option(
        None,
        "--kind",
        "-k",
        help="Force article|note for every file (default: infer from the corpus subfolder)",
    ),
) -> None:
    """Ingest new files. Defaults to data/corpus: notes/ → note, articles/ →
    article, drafts/ skipped. Already-ingested files are left untouched.
    PDF / DOCX / LaTeX / Markdown are supported."""
    cfg = get_config()
    ensure_dirs(cfg)
    db.init_db(cfg)

    if kind is not None and kind not in {"article", "note"}:
        raise typer.BadParameter("--kind must be 'article' or 'note'")

    root = (path or cfg.corpus_dir).resolve()
    outcomes = ingest_tree(cfg, root, kind_override=kind)
    if not outcomes:
        typer.echo(
            f"No supported files found at {root}. Supported: {sorted(SUPPORTED_SUFFIXES)}"
        )
        raise typer.Exit(code=1)

    new = [o for o in outcomes if o.status == "new"]
    existing = [o for o in outcomes if o.status == "existing"]
    failed = [o for o in outcomes if o.status == "failed"]

    for o in new:
        typer.echo(f"[new]  #{o.doc_id:04d}  [{o.kind:7}]  {o.path}")
    for o in failed:
        typer.echo(f"[fail] {o.path}: {o.error}", err=True)

    typer.echo(
        f"\nIngested {len(new)} new, skipped {len(existing)} already-ingested"
        + (f", {len(failed)} failed" if failed else "")
        + "."
    )
    if failed:
        raise typer.Exit(code=2)


@app.command()
def extract(
    doc: int = typer.Option(None, "--doc", "-d", help="Document id; omit to process all unextracted docs"),
    passes: str = typer.Option(
        "concepts,questions",
        "--passes",
        help="Comma-separated list of passes to run (concepts, questions)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-extract even if already extracted (this re-bills Claude)",
    ),
) -> None:
    """Run Claude extraction passes for one or all ingested documents.

    Already-extracted documents are skipped (so a bare `cophilo extract`
    never silently re-bills); pass --force, or name one with --doc --force,
    to deliberately re-run."""
    cfg = get_config()
    db.init_db(cfg)
    pass_tuple = tuple(p.strip() for p in passes.split(",") if p.strip())

    with db.transaction(cfg) as conn:
        if doc is not None:
            row = conn.execute(
                "SELECT id, status FROM documents WHERE id = ?;", (doc,)
            ).fetchone()
            if row is None:
                typer.echo(
                    f"No document #{doc}. Run `cophilo list` to see ingested docs.",
                    err=True,
                )
                raise typer.Exit(code=1)
            if row["status"] == "extracted" and not force:
                typer.echo(
                    f"Document #{doc:04d} is already extracted. "
                    f"Pass --force to re-run (this re-bills Claude)."
                )
                return
            ids = [doc]
        else:
            clause = "" if force else " WHERE status = 'ingested'"
            rows = conn.execute(
                f"SELECT id FROM documents{clause} ORDER BY id;"
            ).fetchall()
            ids = [int(r["id"]) for r in rows]

    if not ids:
        typer.echo("Nothing to extract (all ingested docs are already extracted).")
        return

    total = len(ids)
    for i, did in enumerate(ids, start=1):
        typer.echo(f"[{i}/{total}] extracting document #{did:04d}…")
        try:
            stats = extract_document(cfg, did, passes=pass_tuple)
        except ValueError as e:
            typer.echo(f"  skipped #{did:04d}: {e}", err=True)
            continue
        typer.echo(
            f"  concepts: +{stats.confirmed_mentions} mentions, "
            f"{stats.new_concept_proposals} new-concept proposal(s) queued "
            f"(across {stats.new_concept_labels} distinct label(s)); "
            f"questions: +{stats.question_mentions} mentions, "
            f"{stats.new_questions} new questions"
        )
        typer.echo(
            f"  tokens: {stats.input_tokens} in, {stats.output_tokens} out, "
            f"cache {stats.cache_read_tokens} read / {stats.cache_write_tokens} write"
        )


@app.command(name="list")
def list_docs(
    kind: str = typer.Option(None, "--kind", "-k", help="Filter by article|note"),
) -> None:
    """List ingested documents."""
    cfg = get_config()
    db.init_db(cfg)
    with db.transaction(cfg) as conn:
        rows = db.list_documents(conn, kind=kind)
    if not rows:
        typer.echo("No documents ingested.")
        return
    for row in rows:
        typer.echo(
            f"#{row['id']:04d}  [{row['kind']:7}]  [{row['language'] or '??'}]  "
            f"{row['title'] or '(untitled)'}"
        )


@app.command()
def concepts(
    doc: int = typer.Option(None, "--doc", "-d", help="Only concepts mentioned in this document"),
    pending: bool = typer.Option(
        True,
        "--pending/--no-pending",
        help="Also show new-concept proposals queued by `extract` (not yet confirmed)",
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Emit JSON"),
) -> None:
    """Show the concepts `extract` found — confirmed ones with mention
    counts, plus the new-concept proposals it queued. The extracted graph,
    without opening SQLite."""
    cfg = get_config()
    db.init_db(cfg)
    with db.transaction(cfg) as conn:
        confirmed = db.list_concepts(conn, document_id=doc)
        proposals = db.list_concept_proposals(conn, document_id=doc) if pending else []

    if as_json:
        # Expose a single primary identifier `name` for both confirmed and
        # proposed items so external tooling (--json was explicitly sold for
        # this) can iterate without branching on shape.
        confirmed_out = []
        for r in confirmed:
            d = dict(r)
            d["name"] = (
                d.get("canonical_label_en")
                or d.get("canonical_label_fr")
                or d.get("slug")
            )
            confirmed_out.append(d)
        typer.echo(
            json.dumps(
                {"confirmed": confirmed_out, "proposed": proposals},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    scope = f" in document #{doc:04d}" if doc is not None else ""
    if not confirmed and not proposals:
        typer.echo(
            f"No concepts{scope} yet. Run `cophilo extract` first "
            "(then confirm proposals to promote them)."
        )
        return
    if confirmed:
        typer.echo(f"Confirmed concepts{scope} ({len(confirmed)}):\n")
        for r in confirmed:
            label = r["canonical_label_en"] or r["canonical_label_fr"] or r["slug"]
            typer.echo(f"  [{r['mentions']:>3}×] {label}  ({r['kind']}, {r['slug']})")
            if r["description"]:
                _wrap_field("      ", r["description"].strip())
    if proposals:
        total_mentions = sum(p["count"] for p in proposals)
        typer.echo(
            f"\nProposed (queued by extract, awaiting confirmation) "
            f"({len(proposals)} distinct label(s), {total_mentions} mention(s)):\n"
        )
        for p in proposals:
            typer.echo(f"  [{p['count']:>3}×] {p['name']}")
            if p["description"]:
                _wrap_field("      ", p["description"].strip())


@app.command()
def questions(
    doc: int = typer.Option(None, "--doc", "-d", help="Only questions raised in this document"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Emit JSON"),
) -> None:
    """Show the open questions `extract` surfaced, most-raised first."""
    cfg = get_config()
    db.init_db(cfg)
    with db.transaction(cfg) as conn:
        rows = db.list_questions(conn, document_id=doc)

    if as_json:
        typer.echo(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        return

    scope = f" in document #{doc:04d}" if doc is not None else ""
    if not rows:
        typer.echo(f"No questions{scope} yet. Run `cophilo extract` first.")
        return
    typer.echo(f"Questions{scope} ({len(rows)}):\n")
    for r in rows:
        flag = "" if r["status"] == "open" else f" [{r['status']}]"
        typer.echo(f"  [{r['mentions']:>3}×]{flag} {r['label']}")
        if r["description"]:
            _wrap_field("      ", r["description"].strip())


def _concept_label(row) -> str:
    return row["canonical_label_en"] or row["canonical_label_fr"] or row["slug"]


def _pair_label(row, side: str) -> str:
    """Format an ``a_`` or ``b_`` side of a co-occurrence row as a label."""
    return (
        row[f"{side}_en"]
        or row[f"{side}_fr"]
        or row[f"{side}_slug"]
    )


@app.command()
def graph(
    name: str = typer.Argument(
        None,
        help="Optional: a concept name / slug to drill into. Omit for the corpus-wide overview.",
    ),
    limit: int = typer.Option(15, "--limit", "-n", help="Max rows per section"),
    min_shared: int = typer.Option(
        2,
        "--min-shared",
        help="Minimum shared documents to surface a co-occurring pair (overview only)",
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Emit JSON"),
) -> None:
    """The cross-document concept graph: what your notes are actually *about*.

    Without an argument, prints the corpus-wide overview — top concepts by
    document spread, top co-occurring pairs. With a concept name or slug,
    drills into that concept: the documents it appears in, the concepts
    that share those documents, and the questions raised in them.

    `cophilo concepts` flat-lists per-doc; this is the dual — the graph
    *across* notes (REPORT.md §2.3)."""
    cfg = get_config()
    db.init_db(cfg)
    with db.transaction(cfg) as conn:
        if name is None:
            spread = db.list_concepts_with_spread(conn)[:limit]
            pairs = db.concept_co_occurrences(
                conn, min_shared=min_shared, limit=limit
            )
            if as_json:
                payload = {
                    "concepts": [dict(r) for r in spread],
                    "co_occurrences": [dict(r) for r in pairs],
                }
                typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
                return
            if not spread:
                typer.echo(
                    "No confirmed concepts yet. Run `cophilo extract`, then "
                    "confirm proposals to promote them."
                )
                return
            typer.echo(
                f"Top concepts by document spread ({len(spread)}):\n"
                f"  docs × mentions   concept"
            )
            for r in spread:
                label = _concept_label(r)
                typer.echo(
                    f"  {r['doc_count']:>4} × {r['mentions']:<7}  "
                    f"{label}  ({r['slug']})"
                )
            if pairs:
                typer.echo(
                    f"\nTop co-occurring concept pairs "
                    f"(≥{min_shared} shared documents):\n"
                )
                for p in pairs:
                    a, b = _pair_label(p, "a"), _pair_label(p, "b")
                    typer.echo(f"  [{p['shared']:>3} doc(s)] {a}  ↔  {b}")
            else:
                typer.echo(
                    f"\nNo concept pairs share ≥{min_shared} documents yet. "
                    "Lower --min-shared or capture more notes."
                )
            return

        # --- drill into one concept ------------------------------------------
        concept = db.find_concept(conn, name)
        if concept is None:
            typer.echo(
                f"No confirmed concept matching {name!r}. "
                "Try `cophilo concepts` to see what's there.",
                err=True,
            )
            raise typer.Exit(code=1)
        docs = db.concept_docs(conn, concept["id"])
        neighbors = db.concept_neighbors(conn, concept["id"], limit=limit)
        qs = db.concept_questions(conn, concept["id"], limit=limit)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "concept": dict(concept),
                    "documents": [dict(r) for r in docs],
                    "neighbors": [dict(r) for r in neighbors],
                    "questions": [dict(r) for r in qs],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    label = _concept_label(concept)
    typer.echo(f"Concept: {label}  ({concept['slug']}, {concept['kind']})")
    if concept["description"]:
        _wrap_field("  ", concept["description"].strip())
    typer.echo("")
    if docs:
        typer.echo(f"Appears in {len(docs)} document(s):")
        for d in docs:
            title = d["title"] or Path(d["source_path"]).name
            typer.echo(f"  [{d['mentions']:>3}×] #{d['id']:04d}  ({d['kind']})  {title}")
    else:
        typer.echo("Appears in no documents (no mentions on file).")
    if neighbors:
        typer.echo(
            f"\nCo-occurring concepts ({len(neighbors)}) — concepts in the "
            "same documents:"
        )
        for n in neighbors:
            nl = n["canonical_label_en"] or n["canonical_label_fr"] or n["slug"]
            typer.echo(f"  [{n['shared_docs']:>3} doc(s)] {nl}  ({n['slug']})")
    if qs:
        typer.echo(
            f"\nQuestions raised in those documents ({len(qs)}):"
        )
        for q in qs:
            flag = "" if q["status"] == "open" else f" [{q['status']}]"
            typer.echo(f"  [{q['docs']:>3} doc(s)]{flag} {q['label']}")


@app.command()
def dialog(
    topic: str = typer.Option(None, "--topic", "-t", help="Group this session under a topic (used in the filename/title)"),
    lang: str = typer.Option(None, "--lang", help="Force note language: en | fr (default: auto-detect, offline)"),
    socratic: bool = typer.Option(
        False,
        "--socratic",
        help=(
            "After each committed note, Claude returns ONE sharp question "
            "back (not a summary, not affirmation). Opt-in: each commit is "
            "an API call. The question is echoed and NOT saved into the note."
        ),
    ),
) -> None:
    """Open a note-taking REPL. Each line you type is saved verbatim into
    data/corpus/notes/ as Markdown. Offline by default; ``--socratic`` adds
    an opt-in single-question interlocutor after each commit."""
    cfg = get_config()
    ensure_dirs(cfg)
    if lang is not None and lang not in {"en", "fr"}:
        raise typer.BadParameter("--lang must be 'en' or 'fr'")
    run_dialog(cfg, topic=topic, language=lang, socratic=socratic, echo=typer.echo)


def _term_width(cap: int = 100) -> int:
    return min(cap, max(40, shutil.get_terminal_size((80, 24)).columns))


def _wrap_field(label: str, text: str, *, joiner: str = " ") -> None:
    """Print ``  label: text`` with the text wrapped under a hanging indent
    instead of as one runaway terminal line."""
    indent = "  " + " " * len(label)
    body = textwrap.fill(
        text,
        width=_term_width(),
        initial_indent=f"  {label}",
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )
    typer.echo(body)


def _echo_tokens(result: object) -> None:
    typer.echo(
        f"[tokens: {getattr(result, 'input_tokens', 0)} in, "
        f"{getattr(result, 'output_tokens', 0)} out, "
        f"cache {getattr(result, 'cache_read_tokens', 0)} read / "
        f"{getattr(result, 'cache_write_tokens', 0)} write]",
        err=True,
    )


@app.command()
def propose(
    lang: str = typer.Option(None, "--lang", help="Prompt language: en | fr"),
    max_notes: int = typer.Option(80, "--max-notes", help="Max notes to consider"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept every proposal without prompting"),
) -> None:
    """Scan your notes for a coherent article. On acceptance, create a
    drafts/<slug>/ folder and MOVE the relevant notes into it."""
    cfg = get_config()
    ensure_dirs(cfg)
    db.init_db(cfg)

    typer.echo("Reading your notes …", err=True)
    result = propose_articles(cfg, language=lang, max_notes=max_notes)
    if result.notes_considered == 0:
        typer.echo("No notes to consider. Capture some with `cophilo dialog`, then `cophilo ingest`.")
        return
    if not result.proposals:
        typer.echo(
            f"Considered {result.notes_considered} note(s); no coherent article yet."
        )
        _echo_tokens(result)
        return

    for i, p in enumerate(result.proposals, start=1):
        titles = [result.notes_by_id[n].title for n in p.note_ids if n in result.notes_by_id]
        typer.echo("")
        typer.echo(f"[{i}/{len(result.proposals)}] {p.title}")
        _wrap_field("thesis:    ", p.thesis)
        _wrap_field("rationale: ", p.rationale)
        _wrap_field(f"notes ({len(p.note_ids)}): ", "; ".join(titles))
        if p.outline:
            _wrap_field("outline:   ", " → ".join(p.outline))
        if p.open_questions:
            _wrap_field("open Qs:   ", "; ".join(p.open_questions))

        if not yes and not typer.confirm(
            f"Create draft and move {len(p.note_ids)} note(s)?", default=False
        ):
            typer.echo("  skipped.")
            continue
        slug = p.slug if yes else typer.prompt("  draft folder slug", default=p.slug)
        chosen = p.model_copy(update={"slug": slug})
        draft_dir = accept_proposal(cfg, chosen, result.notes_by_id)
        typer.echo(f"  → created {draft_dir}  ({len(p.note_ids)} notes moved)")
        typer.echo(f"    next: cophilo draft {draft_dir}")

    _echo_tokens(result)


@app.command()
def draft(
    folder: Path = typer.Argument(..., help="A drafts/<slug>/ folder, or just the slug"),
    query: str = typer.Option(None, "--query", "-q", help="PhilArchive query (default: the draft's thesis)"),
    lang: str = typer.Option(None, "--lang", help="Article language: en | fr (default: auto)"),
    limit: int = typer.Option(30, "--limit", "-n", help="Max bibliography works to retrieve"),
    from_synthesis: Path = typer.Option(
        None,
        "--from-synthesis",
        exists=True,
        readable=True,
        dir_okay=False,
        help=(
            "Reuse a saved synthesis JSON (data/syntheses/<slug>.json) as the "
            "bibliography. Inherits the source-quality verdicts so the draft "
            "won't dress speculative grey literature as scholarly convergence."
        ),
    ),
) -> None:
    """Draft an article (.tex) for a draft folder.

    If a synthesis for the draft's thesis was saved (via `cophilo biblio
    synthesize`), it is reused automatically — the bibliography is *not*
    re-fetched and the source-quality verdicts are carried into the draft
    prompt. Otherwise a fresh PhilArchive query is made from the thesis.
    Pass `--from-synthesis <path>` to point at a specific saved synthesis."""
    cfg = get_config()
    ensure_dirs(cfg)
    db.init_db(cfg)

    draft_dir = folder if folder.is_dir() else cfg.corpus_drafts_dir / str(folder)
    if not draft_dir.is_dir():
        raise typer.BadParameter(f"No such draft folder: {draft_dir}")

    typer.echo(f"Drafting from {draft_dir} …", err=True)
    try:
        result = compose_draft(
            cfg,
            draft_dir,
            language=lang,
            query=query,
            limit=limit,
            from_synthesis=from_synthesis,
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    if result.synthesis_used is not None:
        typer.echo(
            f"Reused synthesis: {result.synthesis_used.name} — "
            f"{len(result.entries)} work(s), with tier verdicts.",
            err=True,
        )
    else:
        typer.echo(
            f"Bibliography query: '{result.query}' — {len(result.entries)} work(s)."
        )
    typer.echo(
        f"Wrote {result.tex_path}  "
        f"({len(result.draft.sections)} sections, {len(result.draft.references)} refs, "
        f"language {result.language})"
    )
    typer.echo(
        f"[tokens: {result.input_tokens} in, {result.output_tokens} out, "
        f"cache {result.cache_read_tokens} read / {result.cache_write_tokens} write]",
        err=True,
    )


@app.command()
def review(
    file: Path = typer.Argument(
        ...,
        exists=True,
        readable=True,
        dir_okay=False,
        help="The file to review (.tex, .md, .txt, …)",
    ),
    lang: str = typer.Option(
        None, "--lang", help="Review language: en | fr (default: auto-detect from the file)"
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Remove cophilo review comments from the file and exit (no LLM)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the annotated file to stdout instead of writing it"
    ),
    only: str = typer.Option(
        None,
        "--only",
        help="Keep only these comment kinds, comma-separated "
        "(weakness,question,suggestion,clarity,strength)",
    ),
    fmt: str = typer.Option(
        "inline",
        "--format",
        help="inline = comments woven into the file; sidecar = a separate "
        "<name>.review.md (no mark touches the source)",
    ),
    respond_to: Path = typer.Option(
        None,
        "--respond-to",
        exists=True,
        readable=True,
        dir_okay=False,
        help=(
            "Counter-pass: read a prior sidecar review (with `> reply: …` "
            "lines you added) and produce round 2 (or 3, capped). Marginalia "
            "as conversation; the critic responds to your reply instead of "
            "monologuing twice."
        ),
    ),
    against: Path = typer.Option(
        None,
        "--against",
        exists=True,
        readable=True,
        dir_okay=False,
        help=(
            "Bibliography-aware review: read a saved synthesis JSON "
            "(data/syntheses/<slug>.json) and check the draft's claims "
            "against its tier verdicts and missing-canonical list. Catches "
            "the grey-literature problem at review-time, not just after "
            "publication."
        ),
    ),
) -> None:
    """Critically (but honestly) review a file, weaving line-anchored comments
    into the file itself. Comments are marked, non-rendering, and reversible:
    re-running replaces the prior review and never edits the original lines.

    With ``--respond-to <prior.review.md>``, instead runs a second-round
    counter-pass over a sidecar review you have annotated with ``> reply:``
    lines: the critic engages your replies (concede / sharpen / pivot) and
    writes ``<name>.review-r2.md`` (or ``-r3``). Capped at three rounds.

    With ``--against <synthesis.json>``, also produces per-claim
    bibliography verdicts (supported / unsupported / overclaim /
    contradicted / missing_citation), citing the works the synthesis
    judged canonical-or-peer-reviewed and flagging missing canonical
    literature the draft is exposed to."""
    cfg = get_config()
    if lang is not None and lang not in {"en", "fr"}:
        raise typer.BadParameter("--lang must be 'en' or 'fr'")
    if fmt not in {"inline", "sidecar"}:
        raise typer.BadParameter("--format must be 'inline' or 'sidecar'")

    valid_kinds = {"strength", "weakness", "question", "suggestion", "clarity"}
    only_set: set[str] | None = None
    if only:
        only_set = {k.strip().lower() for k in only.split(",") if k.strip()}
        bad = only_set - valid_kinds
        if bad:
            raise typer.BadParameter(
                f"--only: unknown kind(s) {sorted(bad)}; pick from {sorted(valid_kinds)}"
            )

    if clear:
        changed = clear_review_comments(file)
        side = sidecar_path(file)
        side_removed = False
        if side.exists():
            side.unlink()
            side_removed = True
        if changed or side_removed:
            where = " and ".join(
                w for w, on in (("inline", changed), (side.name, side_removed)) if on
            )
            typer.echo(f"Cleared review comments ({where}) from {file}")
        else:
            typer.echo(f"No review comments in {file}")
        return

    if respond_to is not None and against is not None:
        raise typer.BadParameter(
            "--respond-to and --against are mutually exclusive: --respond-to "
            "is a counter-pass over a prior review, --against runs a fresh "
            "bibliography-aware review."
        )

    if respond_to is not None:
        typer.echo(
            f"Counter-pass: responding to replies in {respond_to.name} …",
            err=True,
        )
        try:
            counter = respond_to_review(
                cfg, file, prior=respond_to, language=lang
            )
        except ValueError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=1) from e
        except RuntimeError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=1) from e
        typer.echo(
            f"Wrote {counter.path.name} — round {counter.round_index}/{MAX_ROUND_DEPTH}, "
            f"{counter.exchanges_count} exchange(s).",
            err=True,
        )
        typer.echo(f"Summary: {counter.round.summary}", err=True)
        _echo_tokens(counter)
        return

    if against is not None:
        typer.echo(
            f"Reviewing {file} against synthesis {against.name} …", err=True
        )
    else:
        typer.echo(f"Reviewing {file} …", err=True)
    try:
        result = review_file(
            cfg,
            file,
            language=lang,
            write=not dry_run,
            only=only_set,
            sidecar=(fmt == "sidecar"),
            against=against,
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    except RuntimeError as e:  # missing backend / CLI / API key
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    if dry_run:
        typer.echo(result.annotated_text)
    tally = result.tally or "no comments"
    typer.echo(
        f"{'Wrote' if result.written else 'Reviewed'} {result.path} — "
        f"{tally}, language {result.language}.",
        err=True,
    )
    typer.echo(f"Verdict: {result.review.summary}", err=True)
    bib_checks = result.review.bibliography_check
    if bib_checks:
        counts: dict[str, int] = {}
        for c in bib_checks:
            counts[c.verdict] = counts.get(c.verdict, 0) + 1
        order = ["unsupported", "contradicted", "overclaim", "missing_citation", "supported"]
        parts = [f"{counts[k]} {k}" for k in order if counts.get(k)]
        typer.echo(
            f"  bibliography check: {', '.join(parts)} "
            f"(against {result.against.name if result.against else '?'})",
            err=True,
        )
    if result.written:
        if result.sidecar:
            typer.echo(f"  the source file was not touched; review is in {result.path.name}", err=True)
        typer.echo(f"  clear with: cophilo review {file} --clear", err=True)
    _echo_tokens(result)


biblio_app = typer.Typer(
    add_completion=False,
    help="Search PhilArchive and synthesize a topic's literature.",
    no_args_is_help=True,
)
app.add_typer(biblio_app, name="biblio")


def _persist_entries(cfg, entries: list[BiblioEntry]) -> None:
    db.init_db(cfg)
    with db.transaction(cfg) as conn:
        for e in entries:
            db.upsert_bibliography(
                conn,
                source=e.source,
                external_id=e.external_id,
                title=e.title,
                authors=e.authors_str() or None,
                journal=e.journal,
                year=e.year,
                abstract=e.abstract,
                doi=e.doi,
            )


def _print_entry(i: int, e: BiblioEntry) -> None:
    typer.echo(f"[{i:>2}] {e.title}")
    meta = " — ".join(
        p for p in (e.authors_str(), e.journal, str(e.year) if e.year else "") if p
    )
    if meta:
        typer.echo(f"     {meta}")
    typer.echo(f"     {e.url}")
    if e.abstract:
        abstract = e.abstract if len(e.abstract) <= 280 else e.abstract[:280].rstrip() + "…"
        typer.echo(f"     {abstract}")


@biblio_app.command("search")
def biblio_search(
    query: str = typer.Argument(..., help="Free-text PhilArchive search query"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Emit JSON (for tooling)"),
    save: bool = typer.Option(True, "--save/--no-save", help="Persist hits into the bibliography table"),
) -> None:
    """Search PhilArchive for titles, authors, journals, and abstracts."""
    cfg = get_config()
    try:
        entries = philarchive.search(cfg, query, limit=limit)
    except Exception as e:  # network / parse failures surface cleanly
        typer.echo(f"PhilArchive search failed: {e}", err=True)
        raise typer.Exit(code=1) from e

    if save and entries:
        _persist_entries(cfg, entries)

    if as_json:
        typer.echo(json.dumps([e.model_dump() for e in entries], ensure_ascii=False, indent=2))
        return

    if not entries:
        typer.echo("No results.")
        return
    typer.echo(f"{len(entries)} result(s) for '{query}':\n")
    for i, e in enumerate(entries, start=1):
        _print_entry(i, e)
        typer.echo("")


@biblio_app.command("synthesize")
def biblio_synthesize(
    topic: str = typer.Option(None, "--topic", "-t", help="Topic description paragraph"),
    topic_file: Path = typer.Option(
        None, "--topic-file", "-f", exists=True, readable=True, help="Read the topic from a file"
    ),
    query: str = typer.Option(
        None, "--query", "-q", help="PhilArchive search query (defaults to the topic text)"
    ),
    limit: int = typer.Option(30, "--limit", "-n", help="Max works to retrieve and feed Claude"),
    lang: str = typer.Option(None, "--lang", help="Prompt language: en | fr"),
    out: Path = typer.Option(None, "--out", "-o", help="Also write the synthesis markdown here"),
    save: bool = typer.Option(True, "--save/--no-save", help="Persist hits into the bibliography table"),
) -> None:
    """Search PhilArchive on a topic and have Claude synthesize the discussion,
    the big questions, and the smaller ones."""
    cfg = get_config()

    if topic_file is not None:
        topic_text = topic_file.read_text(encoding="utf-8").strip()
    elif topic:
        topic_text = topic.strip()
    elif not sys.stdin.isatty():
        topic_text = sys.stdin.read().strip()
    else:
        raise typer.BadParameter("Provide --topic, --topic-file, or pipe the topic on stdin.")
    if not topic_text:
        raise typer.BadParameter("Topic is empty.")

    search_query = (query or topic_text).strip()

    typer.echo(f"Searching PhilArchive: '{search_query}' …", err=True)
    try:
        entries = philarchive.search(cfg, search_query, limit=limit)
    except Exception as e:
        typer.echo(f"PhilArchive search failed: {e}", err=True)
        raise typer.Exit(code=1) from e

    if not entries:
        typer.echo("No works retrieved — try a broader --query.", err=True)
        raise typer.Exit(code=1)
    if save:
        _persist_entries(cfg, entries)

    typer.echo(f"Synthesizing {len(entries)} works with Claude …", err=True)
    try:
        result = synthesize_topic(cfg, topic_text, entries, language=lang)
    except RuntimeError as e:  # missing ANTHROPIC_API_KEY
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    # Persist to the canonical location so `cophilo draft` can reuse the
    # source-quality verdicts without re-billing PhilArchive / Claude. The
    # save_synthesis call also looks up candidate venues from the local
    # memory index (silently no-ops if not built).
    ensure_dirs(cfg)
    json_path, md_path = save_synthesis(
        cfg, topic_text, search_query, result.synthesis, entries
    )
    md = md_path.read_text(encoding="utf-8")
    typer.echo(f"Saved synthesis: {json_path}", err=True)
    typer.echo(f"             md: {md_path}", err=True)
    if out is not None:
        out.write_text(md, encoding="utf-8")
        typer.echo(f"Wrote {out}", err=True)
    typer.echo(md)
    typer.echo(
        f"\n[tokens: {result.input_tokens} in, {result.output_tokens} out, "
        f"cache {result.cache_read_tokens} read / {result.cache_write_tokens} write]",
        err=True,
    )


memory_app = typer.Typer(
    add_completion=False,
    help="Local semantic memory over the journals catalog (sqlite-vec).",
    no_args_is_help=True,
)
app.add_typer(memory_app, name="memory")


@memory_app.command("index")
def memory_index() -> None:
    """(Re)build the journals vector store from data/journals.yaml."""
    from cophilo.memory import build

    cfg = get_config()
    typer.echo("Embedding journals catalog (first run downloads the model) …", err=True)
    try:
        n = build(cfg)
    except FileNotFoundError as e:
        typer.echo(
            f"No journals catalog to index: {e}. Expected data/journals.yaml.",
            err=True,
        )
        raise typer.Exit(code=1) from e
    typer.echo(f"Indexed {n} journals → {cfg.memory_db_path}")


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(..., help="Topic / subfield / abstract"),
    limit: int = typer.Option(8, "--limit", "-n", help="Max journals"),
    open_access_only: bool = typer.Option(
        False, "--oa", help="Restrict to open-access journals"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Emit JSON"),
) -> None:
    """Semantically search the journals catalog."""
    from cophilo.memory import search as memory_search_fn

    cfg = get_config()
    try:
        results = memory_search_fn(
            cfg, query, limit=limit, open_access_only=open_access_only
        )
    except FileNotFoundError as e:
        typer.echo(
            f"No journals catalog: {e}. Expected data/journals.yaml — add it, "
            "then run `cophilo memory index`.",
            err=True,
        )
        raise typer.Exit(code=1) from e
    except ValueError as e:  # empty query, etc.
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    if as_json:
        typer.echo(json.dumps(results, ensure_ascii=False, indent=2))
        return
    if not results:
        typer.echo("No matches.")
        return
    typer.echo(f"{len(results)} match(es) for '{query}':\n")
    for i, r in enumerate(results, start=1):
        oa = " [OA]" if r["open_access"] else ""
        typer.echo(f"[{i:>2}] {r['name']}{oa}  (score {r['score']})")
        if r.get("scope"):
            typer.echo(f"     {r['scope']}")
        if r.get("typical_length"):
            typer.echo(f"     length: {r['typical_length']}")
        if r.get("url"):
            typer.echo(f"     {r['url']}")
        typer.echo("")


if __name__ == "__main__":  # pragma: no cover
    app()
