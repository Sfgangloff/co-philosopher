"""Offline, verbatim note capture.

``cophilo dialog`` opens a REPL. You type a note over as many lines as you
like — Enter is just a newline *within* the note — and a **blank line**
(or ``/save``) commits it as one coherent unit to a Markdown file in
``data/corpus/notes/``. One session is one file by default; ``/new`` starts a
fresh file. This keeps notes substantial multi-paragraph units instead of a
confetti of one-line files, so ``extract``/``propose`` reason over real
arguments. No LLM, no network — the only smarts are slugging the filename and
offline language detection for the frontmatter. ``cophilo ingest`` later
picks the notes up like any other corpus file.

Session commands: ``/save`` commit the note · ``/done`` end · ``/new`` start
a fresh note file · ``/cancel`` discard the in-progress note · ``/help``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import frontmatter
from slugify import slugify

from cophilo.config import Config
from cophilo.ingest.normalize import detect_language

Clock = Callable[[], datetime]
_TITLE_MAX = 70


def _note_path(cfg: Config, topic: str | None, now: datetime) -> Path:
    """A unique ``<date-time>-<slug>.md`` path inside corpus/notes."""
    stamp = now.strftime("%Y-%m-%d-%H%M")
    slug = slugify(topic, max_length=60) if topic else "note"
    slug = slug or "note"
    base = cfg.corpus_notes_dir / f"{stamp}-{slug}.md"
    candidate = base
    n = 2
    while candidate.exists():
        candidate = cfg.corpus_notes_dir / f"{stamp}-{slug}-{n}.md"
        n += 1
    return candidate


def _first_line_title(text: str) -> str:
    line = text.strip().splitlines()[0].strip() if text.strip() else "Untitled note"
    return line if len(line) <= _TITLE_MAX else line[:_TITLE_MAX].rstrip() + "…"


@dataclass
class DialogSession:
    """Holds the state of one capture session. File I/O lives here so the
    REPL loop (and tests) stay trivial."""

    cfg: Config
    topic: str | None = None
    language: str | None = None
    clock: Clock = datetime.now
    current_path: Path | None = None
    files: list[Path] = field(default_factory=list)
    total_entries: int = 0

    def new_note(self) -> None:
        """Force the next entry to begin a fresh note file."""
        self.current_path = None

    def cancel(self) -> Path | None:
        """Discard the in-progress note file, if any, and reset."""
        path = self.current_path
        if path is not None and path.exists():
            path.unlink()
            self.files = [p for p in self.files if p != path]
        self.current_path = None
        return path

    def add(self, text: str) -> tuple[str, Path]:
        """Append ``text`` verbatim. Returns ('saved'|'appended', path)."""
        self.cfg.corpus_notes_dir.mkdir(parents=True, exist_ok=True)
        self.total_entries += 1
        if self.current_path is None:
            path = _note_path(self.cfg, self.topic, self.clock())
            language = self.language or detect_language(
                text, default=self.cfg.default_language
            )
            post = frontmatter.Post(
                text,
                title=self.topic or _first_line_title(text),
                created=self.clock().isoformat(timespec="seconds"),
                kind="note",
                language=language,
                **({"topic": self.topic} if self.topic else {}),
            )
            path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
            self.current_path = path
            self.files.append(path)
            return "saved", path

        path = self.current_path
        # Exactly one blank line between notes. Naively prepending "\n\n" stacks
        # on top of the previous write's trailing newline, leaving \n\n\n runs.
        existing = path.read_text(encoding="utf-8")
        path.write_text(
            existing.rstrip("\n") + "\n\n" + text.rstrip() + "\n",
            encoding="utf-8",
        )
        return "appended", path

    def summary(self) -> str:
        if not self.files:
            return "No notes captured."
        return (
            f"Saved {self.total_entries} entr"
            f"{'y' if self.total_entries == 1 else 'ies'} "
            f"across {len(self.files)} file"
            f"{'' if len(self.files) == 1 else 's'} in {self.cfg.corpus_notes_dir}"
        )


_BANNER = (
    "cophilo dialog — write a note over as many lines as you want.\n"
    "A BLANK LINE (or /save) commits it as one note. Enter alone is just a "
    "newline within the note.\n"
    "To leave: /done  (a bare `exit` / `quit` between notes also works; "
    "Ctrl-D / Ctrl-C — a pending note is saved first).\n"
    "Other commands: /new (start a new note file)  /cancel (drop the "
    "in-progress note)  /help"
)
_HELP = (
    "(blank line)  commit the note you've been typing\n"
    "/save    commit the note you've been typing (same as a blank line)\n"
    "/done    finish and leave notes mode (commits any pending note first)\n"
    "/new     commit the pending note, then start a new note file\n"
    "/cancel  discard the in-progress note (unsaved lines or current file)\n"
    "/help    show this help\n"
    "Ctrl-D or Ctrl-C also leaves — a pending note is committed first."
)


def run_dialog(
    cfg: Config,
    *,
    topic: str | None = None,
    language: str | None = None,
    input_fn: Callable[[str], str] = input,
    echo: Callable[[str], None] = print,
    clock: Clock = datetime.now,
) -> DialogSession:
    """Run the capture loop. ``input_fn``/``echo``/``clock`` are injectable
    so the loop is testable without a TTY."""
    session = DialogSession(cfg=cfg, topic=topic, language=language, clock=clock)
    echo(_BANNER)
    if topic:
        echo(f"(topic: {topic})")

    buffer: list[str] = []

    def commit() -> None:
        """Flush the buffered lines as one coherent note."""
        if not buffer:
            return
        note = "\n".join(buffer).strip()
        buffer.clear()
        if not note:
            return
        status, path = session.add(note)
        echo(f"  ↳ {status} {path.name}")

    while True:
        try:
            line = input_fn("  … " if buffer else "note> ")
        except (EOFError, KeyboardInterrupt):
            echo("")
            break

        # A blank line commits the note you've been writing.
        if not line.strip():
            commit()
            continue

        text = line.strip()
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            if cmd in {"/done", "/quit", "/exit"}:
                break
            if cmd == "/save":
                if buffer:
                    commit()
                else:
                    echo("  ↳ nothing to save yet")
            elif cmd == "/new":
                commit()  # don't lose a pending note at the boundary
                session.new_note()
                echo("  ↳ new note file started")
            elif cmd == "/cancel":
                if buffer:
                    n = len(buffer)
                    buffer.clear()
                    echo(f"  ↳ discarded {n} unsaved line(s)")
                else:
                    path = session.cancel()
                    echo(
                        f"  ↳ discarded {path.name}"
                        if path
                        else "  ↳ nothing to discard"
                    )
            elif cmd == "/help":
                echo(_HELP)
            else:
                echo(f"  ↳ unknown command {cmd!r} (try /help)")
            continue

        # The outer prompt uses `exit`; here the leave command is `/done`. A
        # bare single-word `exit`/`quit`/`done` is a near-certain muscle-memory
        # mismatch — silently committing it as a note ends every first-time
        # session with a stray "exit" paragraph. If the user is between notes
        # we leave; if they're mid-note, we ask rather than guess.
        if text.lower() in {"exit", "quit", "done"}:
            if not buffer:
                echo(f"  ↳ '{text}' alone — leaving (the in-dialog command is /done).")
                break
            echo(
                f"  ↳ '{text}' alone — type /done to leave, /save to commit, "
                "or keep writing to make it part of the note."
            )
            continue

        # Otherwise this line is part of the note being written.
        buffer.append(line.rstrip())

    commit()  # a note in progress at exit is kept, not lost
    echo(session.summary())
    echo("← left notes mode (back at the cophilo prompt).")
    return session
