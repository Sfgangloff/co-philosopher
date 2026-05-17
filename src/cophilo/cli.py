"""Typer entrypoint for the cophilo CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from cophilo.biblio import philarchive
from cophilo.biblio.schemas import BiblioEntry
from cophilo.biblio.synthesize import render_markdown, synthesize_topic
from cophilo.config import ensure_dirs, get_config
from cophilo.db import models as db
from cophilo.extract.passes import extract_document
from cophilo.ingest.dispatch import (
    SUPPORTED_SUFFIXES,
    UnsupportedFormatError,
    ingest_file,
    iter_supported,
)

app = typer.Typer(
    add_completion=False,
    help="Co-philosopher: interactive philosophy assistant.",
    no_args_is_help=True,
)


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
def ingest(
    path: Path = typer.Argument(..., exists=True, readable=True, help="File or directory to ingest"),
    kind: str = typer.Option("article", "--kind", "-k", help="article | note"),
) -> None:
    """Ingest a file or directory. PDF / DOCX / LaTeX are supported."""
    cfg = get_config()
    ensure_dirs(cfg)
    db.init_db(cfg)

    if kind not in {"article", "note"}:
        raise typer.BadParameter("--kind must be 'article' or 'note'")

    files = list(iter_supported(path))
    if not files:
        typer.echo(
            f"No supported files found at {path}. Supported: {sorted(SUPPORTED_SUFFIXES)}"
        )
        raise typer.Exit(code=1)

    failures: list[tuple[Path, str]] = []
    for f in files:
        try:
            doc_id = ingest_file(cfg, f, kind=kind)
            typer.echo(f"[ok]   #{doc_id:04d}  {f}")
        except UnsupportedFormatError as e:
            failures.append((f, str(e)))
            typer.echo(f"[skip] {f}: {e}", err=True)
        except Exception as e:  # pragma: no cover — surface unexpected failures
            failures.append((f, str(e)))
            typer.echo(f"[fail] {f}: {e}", err=True)

    typer.echo(f"\nIngested {len(files) - len(failures)}/{len(files)} files.")
    if failures:
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


if __name__ == "__main__":  # pragma: no cover
    app()
