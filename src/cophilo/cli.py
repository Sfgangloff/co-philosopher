"""Typer entrypoint for the cophilo CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from cophilo.backup import DEFAULT_REPO_NAME, BackupError, backup_corpus
from cophilo.biblio import philarchive
from cophilo.biblio.schemas import BiblioEntry
from cophilo.biblio.synthesize import render_markdown, synthesize_topic
from cophilo.config import ensure_dirs, get_config
from cophilo.db import models as db
from cophilo.draft import accept_proposal, compose_draft, propose_articles
from cophilo.extract.passes import extract_document
from cophilo.ingest.dispatch import SUPPORTED_SUFFIXES, ingest_tree
from cophilo.notes import run_dialog

app = typer.Typer(
    add_completion=False,
    help="Co-philosopher: interactive philosophy assistant.",
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
) -> None:
    """Run Claude extraction passes for one or all ingested documents."""
    cfg = get_config()
    db.init_db(cfg)
    pass_tuple = tuple(p.strip() for p in passes.split(",") if p.strip())

    with db.transaction(cfg) as conn:
        if doc is not None:
            ids = [doc]
        else:
            rows = conn.execute(
                "SELECT id FROM documents WHERE status = 'ingested' ORDER BY id;"
            ).fetchall()
            ids = [int(r["id"]) for r in rows]

    if not ids:
        typer.echo("Nothing to extract.")
        return

    total = len(ids)
    for i, did in enumerate(ids, start=1):
        typer.echo(f"[{i}/{total}] extracting document #{did:04d}…")
        stats = extract_document(cfg, did, passes=pass_tuple)
        typer.echo(
            f"  concepts: +{stats.confirmed_mentions} mentions, "
            f"{stats.new_concept_proposals} new-concept proposals queued; "
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
def dialog(
    topic: str = typer.Option(None, "--topic", "-t", help="Group this session under a topic (used in the filename/title)"),
    lang: str = typer.Option(None, "--lang", help="Force note language: en | fr (default: auto-detect, offline)"),
) -> None:
    """Open an offline note-taking REPL. Each line you type is saved verbatim
    into data/corpus/notes/ as Markdown. No LLM, no network."""
    cfg = get_config()
    ensure_dirs(cfg)
    if lang is not None and lang not in {"en", "fr"}:
        raise typer.BadParameter("--lang must be 'en' or 'fr'")
    run_dialog(cfg, topic=topic, language=lang, echo=typer.echo)


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
        typer.echo(f"  thesis:    {p.thesis}")
        typer.echo(f"  rationale: {p.rationale}")
        typer.echo(f"  notes ({len(p.note_ids)}): " + "; ".join(titles))
        if p.outline:
            typer.echo("  outline:   " + " → ".join(p.outline))
        if p.open_questions:
            typer.echo("  open Qs:   " + "; ".join(p.open_questions))

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
) -> None:
    """Draft an article (.tex) for a draft folder: pull a PhilArchive
    bibliography from the thesis and have Claude write it from your notes."""
    cfg = get_config()
    ensure_dirs(cfg)
    db.init_db(cfg)

    draft_dir = folder if folder.is_dir() else cfg.corpus_drafts_dir / str(folder)
    if not draft_dir.is_dir():
        raise typer.BadParameter(f"No such draft folder: {draft_dir}")

    typer.echo(f"Drafting from {draft_dir} …", err=True)
    try:
        result = compose_draft(cfg, draft_dir, language=lang, query=query, limit=limit)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    typer.echo(f"Bibliography query: '{result.query}' — {len(result.entries)} work(s).")
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

    md = render_markdown(topic_text, search_query, result.synthesis, entries)
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
    n = build(cfg)
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
    results = memory_search_fn(
        cfg, query, limit=limit, open_access_only=open_access_only
    )
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
