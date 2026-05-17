"""The bare ``cophilo`` home screen and the ``help`` catalogue.

Layout mirrors the Claude Code splash: a title on top, a discretised
"philosopher" portrait on the left, and a reminder to type ``help`` on the
right. ``help`` is introspected straight from the Typer app, so the command
and option listing can never drift from the real CLI.

Everything here is offline: no LLM, no network.
"""

from __future__ import annotations

import shlex
import sys

import click
import typer
from rich.align import Align
from rich.box import ROUNDED
from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from typer.main import get_command

from cophilo import __version__

# An ouroboros — the snake biting its own tail: a fitting emblem for a tool
# that turns its own notes back into questions. Generated procedurally as a
# tapered ring with a "bite" gap and a small head; single-width glyphs only.
_PORTRAIT = "\n" + "\n".join([
    "        ░▒▒▓▓▓▓▓▒░",
    "     ▒▓████████▓▓█",
    "   ▒██████▓▒▒▒▓▓▓░ ░░",
    "  ▓████▒░           ▒▓░",
    " ▓███▓░              ▓█▒",
    "░████░               ░██░",
    "░████                ▒██▓",
    "░████░               ▓██▓",
    " ▓███▓░            ░▓███▒",
    "  ▓████▒░        ░▒████▓",
    "   ▒██████▓▒▒▒▒▓██████▒",
    "     ▒▓████████████▓▒",
    "        ░▒▒▓▓▓▓▒▒░",
]) + "\n"

_TAGLINE = "ingest · extract · dialog · propose · draft — key-free, offline-first"


def _commands(app: typer.Typer) -> list[tuple[str, click.Command]]:
    """Every leaf command as ``("biblio search", cmd)``, depth-first."""
    out: list[tuple[str, click.Command]] = []

    def rec(node: click.Command, path: list[str]) -> None:
        if isinstance(node, click.Group):
            for name in sorted(node.commands):
                rec(node.commands[name], path + [name])
        else:
            out.append((" ".join(path), node))

    rec(get_command(app), [])
    return out


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def render_help(app: typer.Typer) -> Group:
    """A Rich renderable: every command, its description, and its options."""
    blocks: list = []
    for path, cmd in _commands(app):
        if path == "help":
            continue
        header = Text()
        # `backup` is highlighted (purple) so the safety-net command stands out.
        header.append(
            f"cophilo {path}",
            style="bold magenta" if path == "backup" else "bold cyan",
        )
        desc = _first_line(cmd.help)
        if desc:
            header.append(f"  — {desc}", style="white")
        blocks.append(header)

        opts = Table.grid(padding=(0, 2))
        opts.add_column(style="green", no_wrap=True)
        opts.add_column(style="dim white")
        has_rows = False
        for p in cmd.params:
            if isinstance(p, click.Option):
                flags = ", ".join(p.opts + p.secondary_opts)
            else:  # argument
                flags = f"<{p.name}>"
            help_text = _first_line(getattr(p, "help", None)) or (
                "(required)" if getattr(p, "required", False) else ""
            )
            opts.add_row(f"  {flags}", help_text)
            has_rows = True
        if has_rows:
            blocks.append(opts)
        blocks.append(Text(""))

    return Group(
        Text("cophilo — commands & options\n", style="bold"),
        *blocks,
        Text("Run any of these as `cophilo <command> [options]`.", style="dim"),
    )


def render_home(app: typer.Typer) -> Group:
    title = Align.center(
        Text(f" cophilo v{__version__} ", style="bold black on cyan")
    )

    portrait = Panel(
        Align.center(Text(_PORTRAIT.strip("\n"), style="cyan")),
        box=ROUNDED,
        border_style="cyan",
        padding=(1, 2),
    )

    teaser = Table.grid(padding=(0, 1))
    teaser.add_column(style="bold green", no_wrap=True)
    teaser.add_column(style="white")
    for name, blurb in (
        ("dialog", "capture notes (offline REPL)"),
        ("ingest", "load corpus/ — only what's new"),
        ("propose", "find an article hiding in your notes"),
        ("draft", "notes + bibliography → article.tex"),
    ):
        teaser.add_row(name, blurb)

    right = Panel(
        Group(
            Text("A co-philosopher in your terminal.", style="italic"),
            Text(""),
            teaser,
            Text(""),
            Text.assemble(
                ("Type ", "white"),
                ("help", "bold yellow"),
                (" for every command, with options and descriptions.", "white"),
            ),
            Text.assemble(
                ("Type ", "dim"),
                ("exit", "bold dim"),
                (" to leave.", "dim"),
            ),
        ),
        title="getting started",
        box=ROUNDED,
        border_style="cyan",
        padding=(1, 2),
    )

    return Group(
        title,
        Text(""),
        Columns([portrait, right], expand=True, equal=False),
        Text(""),
        Align.center(Text(_TAGLINE, style="dim")),
    )


def print_help(app: typer.Typer, console: Console | None = None) -> None:
    (console or Console()).print(render_help(app))


def run_home(
    app: typer.Typer,
    *,
    console: Console | None = None,
    input_fn=None,
) -> None:
    """Render the home screen, then (on a TTY) a light command prompt.

    Off a TTY — pipes, CI, ``CliRunner`` — it prints the splash and returns,
    so it never blocks. ``input_fn`` is injectable for tests."""
    console = console or Console()
    console.print(render_home(app))

    if input_fn is None and not sys.stdin.isatty():
        console.print("\n[dim]Run `cophilo help` for the full command list.[/dim]")
        return
    ask = input_fn or (lambda: input("\ncophilo> "))
    root = get_command(app)

    while True:
        try:
            line = ask()
        except (EOFError, KeyboardInterrupt):
            console.print("")
            break
        s = (line or "").strip()
        if not s:
            continue
        low = s.lower()
        if low in {"exit", "quit", ":q"}:
            break
        if low in {"help", "?", "/help", "h"}:
            print_help(app, console)
            continue
        argv = shlex.split(s)
        if argv and argv[0] == "cophilo":
            argv = argv[1:]  # tolerate a pasted "cophilo " prefix
        if not argv:
            continue
        try:
            root.main(argv, prog_name="cophilo", standalone_mode=False)
        except SystemExit:
            pass
        except click.ClickException as e:
            e.show()
        except click.Abort:
            console.print("[dim]aborted[/dim]")
        except Exception as e:  # keep the shell alive on command failure
            console.print(f"[red]error:[/red] {e}")

    console.print("[dim]bye.[/dim]")
