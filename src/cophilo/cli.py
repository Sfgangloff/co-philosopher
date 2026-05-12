"""Typer entrypoint for the cophilo CLI."""

from __future__ import annotations

from pathlib import Path

import typer

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


if __name__ == "__main__":  # pragma: no cover
    app()
