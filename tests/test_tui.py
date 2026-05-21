"""Tests for the `cophilo` home screen and the `help` catalogue.

Pure offline UI: no DB, no network, no API. The REPL is driven through an
injected ``input_fn`` and a Rich console writing to a string buffer.
"""

from __future__ import annotations

import io

from rich.console import Console
from typer.testing import CliRunner

from cophilo import __version__
from cophilo.cli import app
from cophilo.tui import _first_sentence, render_help, run_home

runner = CliRunner()


def test_bare_cophilo_shows_splash_and_exits():
    res = runner.invoke(app, [])
    assert res.exit_code == 0, res.output
    assert f"cophilo v{__version__}" in res.output
    assert "help" in res.output  # the reminder is present
    assert "dialog" in res.output  # getting-started teaser
    # non-TTY (CliRunner) → static splash, no hang
    assert "cophilo help" in res.output


def test_help_catalogue_lists_commands_and_options():
    res = runner.invoke(app, ["help"])
    assert res.exit_code == 0, res.output
    for command in (
        "cophilo dialog",
        "cophilo ingest",
        "cophilo propose",
        "cophilo draft",
        "cophilo biblio search",
        "cophilo memory search",
    ):
        assert command in res.output, command
    # options and arguments with their flags are rendered
    assert "--topic" in res.output
    assert "--limit" in res.output
    assert "<folder>" in res.output  # draft's positional argument


def test_first_sentence_unwraps_paragraph_not_physical_line():
    # A hard-wrapped two-line docstring must not be cut at the newline.
    doc = (
        "Back up data/corpus to a separate private git repo, creating it under\n"
        "your own GitHub account on first run. Fork-friendly: nothing hard-coded."
    )
    s = _first_sentence(doc)
    assert s == (
        "Back up data/corpus to a separate private git repo, creating it "
        "under your own GitHub account on first run."
    )
    assert not s.endswith("under")  # the old mid-sentence truncation is gone
    assert _first_sentence(None) == "" and _first_sentence("  ") == ""


def test_help_catalogue_descriptions_are_whole_sentences():
    res = runner.invoke(app, ["help"])
    assert res.exit_code == 0, res.output
    # `backup`'s description reaches its first full stop, not the line break.
    assert "on first run." in res.output


def test_render_help_excludes_help_itself():
    buf = io.StringIO()
    Console(file=buf, width=100, force_terminal=False).print(render_help(app))
    out = buf.getvalue()
    assert "commands & options" in out
    assert "cophilo init" in out
    assert "cophilo help" not in out  # the catalogue omits itself


def test_repl_dispatch_help_unknown_and_exit():
    script = iter(["help", "cophilo totally-not-a-command", "", "exit"])
    buf = io.StringIO()
    console = Console(file=buf, width=100, force_terminal=False)

    run_home(app, console=console, input_fn=lambda: next(script))

    out = buf.getvalue()
    assert f"cophilo v{__version__}" in out  # splash rendered
    assert "commands & options" in out  # `help` handled in-loop
    assert "bye." in out  # clean exit
    # an unknown command did not kill the loop (we still reached 'bye.')


def test_repl_runs_a_real_subcommand(tmp_path, monkeypatch):
    monkeypatch.setenv("COPHILO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPHILO_DB_PATH", str(tmp_path / "data" / "db" / "c.sqlite"))
    from cophilo.config import get_config

    get_config.cache_clear()
    script = iter(["init", "exit"])
    buf = io.StringIO()
    console = Console(file=buf, width=100, force_terminal=False)

    run_home(app, console=console, input_fn=lambda: next(script))

    assert (tmp_path / "data" / "db" / "c.sqlite").exists()
    get_config.cache_clear()
