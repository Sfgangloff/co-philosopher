"""Offline, verbatim note capture.

``cophilo dialog`` opens a REPL: every line you type is appended, exactly as
written, to a Markdown file in ``data/corpus/notes/``. No LLM, no network —
the only smarts are slugging the filename and offline language detection for
the frontmatter. ``cophilo ingest`` later picks the notes up like any other
corpus file.

Session commands: ``/done`` end · ``/new`` start a fresh note file ·
``/cancel`` discard the current note file · ``/help``.
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
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n\n" + text.rstrip() + "\n")
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
    "cophilo dialog — type a note and press Enter to save it.\n"
    "To leave notes mode: /done  (or Ctrl-D / Ctrl-C — your work is already saved).\n"
    "Other commands: /new (start a new note)  /cancel (discard current)  /help"
)
_HELP = (
    "/done    finish and leave notes mode (back to the cophilo prompt)\n"
    "/new     start a new note file (next line begins a fresh note)\n"
    "/cancel  delete the current note file and start over\n"
    "/help    show this help\n"
    "Ctrl-D or Ctrl-C also leaves — every saved note is kept."
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

    while True:
        try:
            line = input_fn("note> ")
        except (EOFError, KeyboardInterrupt):
            echo("")
            break

        text = line.strip()
        if not text:
            continue

        if text.startswith("/"):
            cmd = text.split()[0].lower()
            if cmd in {"/done", "/quit", "/exit"}:
                break
            if cmd == "/new":
                session.new_note()
                echo("  ↳ new note started")
            elif cmd == "/cancel":
                path = session.cancel()
                echo(f"  ↳ discarded {path.name}" if path else "  ↳ nothing to discard")
            elif cmd == "/help":
                echo(_HELP)
            else:
                echo(f"  ↳ unknown command {cmd!r} (try /help)")
            continue

        status, path = session.add(text)
        echo(f"  ↳ {status} {path.name}")

    echo(session.summary())
    echo("← left notes mode (back at the cophilo prompt).")
    return session
