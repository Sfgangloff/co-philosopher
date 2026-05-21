"""End-to-end smoke tests for the ingest pipeline.

Generates fixtures on the fly so the repo stays free of binary blobs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import fitz
import frontmatter
import pypandoc
import pytest

from cophilo.config import ensure_dirs, get_config
from cophilo.db import models as db
from cophilo.ingest.dispatch import ingest_file, ingest_tree


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point cophilo at a tmp data dir so each test runs in isolation."""
    data_dir = tmp_path / "data"
    db_path = data_dir / "db" / "cophilo.sqlite"
    monkeypatch.setenv("COPHILO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COPHILO_DB_PATH", str(db_path))
    get_config.cache_clear()
    cfg = get_config()
    ensure_dirs(cfg)
    db.init_db(cfg)
    yield cfg
    get_config.cache_clear()


def _write_pdf(path: Path, body_lines: list[str], title: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), title, fontsize=14)
    page.insert_text((72, 110), "\n".join(body_lines), fontsize=11)
    doc.save(path)
    doc.close()


def _write_docx(path: Path, markdown_body: str) -> None:
    pypandoc.convert_text(
        markdown_body,
        to="docx",
        format="markdown",
        outputfile=str(path),
        extra_args=[],
    )


def _write_tex(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# --- tests ---------------------------------------------------------------


def test_ingest_pdf(isolated_data_dir, tmp_path):
    cfg = isolated_data_dir
    src = tmp_path / "sample.pdf"
    _write_pdf(
        src,
        [
            "This essay considers the question of free will.",
            "Determinism, by contrast, denies that genuine choice is possible.",
        ],
        title="On Free Will",
    )

    doc_id = ingest_file(cfg, src)
    assert doc_id > 0

    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM documents WHERE id = ?;", (doc_id,)).fetchone()
        assert row["language"] == "en"
        assert row["status"] == "ingested"
        assert "free will" in (row["title"] or "").lower()

    normalized = Path(row["normalized_path"])
    assert normalized.exists()
    post = frontmatter.loads(normalized.read_text())
    assert post["language"] == "en"
    assert "free will" in post.content.lower()


def test_ingest_docx_fr(isolated_data_dir, tmp_path):
    cfg = isolated_data_dir
    src = tmp_path / "note.docx"
    _write_docx(
        src,
        "# La conscience de soi\n\n"
        "Cette note examine la conscience de soi et son rapport au libre arbitre. "
        "La question demeure ouverte : sommes-nous libres de choisir nos pensées ?\n",
    )

    doc_id = ingest_file(cfg, src, kind="note")
    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM documents WHERE id = ?;", (doc_id,)).fetchone()
    assert row["language"] == "fr"
    assert row["kind"] == "note"


def test_ingest_tex_with_concept_hints(isolated_data_dir, tmp_path):
    cfg = isolated_data_dir
    src = tmp_path / "essay.tex"
    _write_tex(
        src,
        r"""
\documentclass{article}
\title{On Phenomenology}
\newcommand{\concept}[1]{\emph{#1}}
\begin{document}
\maketitle
We discuss \concept{intentionality} and \concept{epoch\'e} as
core notions of phenomenology, owing largely to Husserl.
\concept{intentionality} returns later in the text.
\end{document}
""".strip(),
    )

    doc_id = ingest_file(cfg, src)
    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM documents WHERE id = ?;", (doc_id,)).fetchone()
    import json

    meta = json.loads(row["metadata_json"])
    hints = meta["concept_hints"]
    # deduped, order-preserving
    assert hints[0] == "intentionality"
    assert any("epoch" in h for h in hints)
    assert hints.count("intentionality") == 1


def test_reingest_is_idempotent(isolated_data_dir, tmp_path):
    cfg = isolated_data_dir
    src = tmp_path / "sample.pdf"
    _write_pdf(src, ["Some philosophical content here."], title="Untitled Essay")

    first = ingest_file(cfg, src)
    second = ingest_file(cfg, src)
    assert first == second

    with sqlite3.connect(cfg.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM documents;").fetchone()[0]
    assert n == 1


def test_ingest_tree_skips_backup_readme(isolated_data_dir):
    cfg = isolated_data_dir
    # `cophilo backup` drops this marker at the corpus root.
    (cfg.corpus_dir / "README.md").write_text(
        "# co-philosopher backup\n\nAutomated private backup.\n", encoding="utf-8"
    )
    real = cfg.corpus_notes_dir / "thought.md"
    real.write_text("Boredom has an intentional structure.\n", encoding="utf-8")

    outcomes = ingest_tree(cfg, cfg.corpus_dir)
    paths = {o.path.name for o in outcomes}
    assert "thought.md" in paths
    assert "README.md" not in paths  # backup metadata, not an [article]
