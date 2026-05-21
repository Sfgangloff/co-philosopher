"""Back up everything built from your notes/articles to a *separate,
private* git repository.

The corpus and everything derived from it (`normalized/`, `rendered/`,
`proposals/`, and the extraction DB `db/cophilo.sqlite`) are gitignored by
the main repo — they're personal. ``data/`` is therefore kept as its own
independent git repo whose working tree tracks exactly those paths, and
``cophilo backup``:

1. resolves the GitHub owner from the authenticated ``gh`` CLI (so a fork
   backs up to *your* account, nothing is hard-coded),
2. creates ``<owner>/co-philosopher-backup`` **private** if it doesn't exist,
3. initialises ``data/`` as a git repo on first run,
4. commits the backed-up paths and pushes to the backup remote.

The journals memory index (``db/memory.sqlite``) and ``journals.yaml`` are
*not* backed up: they derive from a source already tracked in the main repo.

Pass ``--remote <url>`` to skip ``gh`` entirely and push anywhere (self-host,
GitLab, a bare repo …) — keeps it usable by anyone.

The command runner is injected so the orchestration is unit-tested without a
real ``git``/``gh`` or network.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cophilo.config import Config

DEFAULT_REPO_NAME = "co-philosopher-backup"

_BACKUP_README = """\
# co-philosopher backup

Automated, **private** backup of a co-philosopher workspace: the corpus
(notes, articles, drafts) and everything built from it (normalized text,
rendered output, proposals, the extraction database). Created and updated by
`cophilo backup`. This is its own git repository, independent of the
co-philosopher tool repo.
"""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(Protocol):
    """Runs a subprocess. Injected so tests need no real git/gh/network."""

    def __call__(
        self, args: list[str], *, cwd: Path | None = None
    ) -> RunResult: ...


def default_runner(args: list[str], *, cwd: Path | None = None) -> RunResult:
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=120, check=False
        )
    except FileNotFoundError:
        return RunResult(127, "", f"{args[0]}: command not found")
    return RunResult(p.returncode, p.stdout, p.stderr)


@dataclass(frozen=True)
class BackupResult:
    remote_url: str
    slug: str | None
    repo_created: bool
    initialised: bool
    committed: bool
    pushed: bool
    message: str

    def summary(self) -> str:
        bits = []
        if self.repo_created and self.slug:
            bits.append(f"created private repo {self.slug}")
        if self.initialised:
            bits.append("initialised data/corpus as a git repo")
        bits.append("committed corpus" if self.committed else "no changes to commit")
        bits.append("pushed" if self.pushed else "push skipped")
        return f"Backup → {self.remote_url}\n  " + "\n  ".join(bits)


class BackupError(RuntimeError):
    """Raised with an actionable message when backup can't proceed."""


def _gh_owner(run: CommandRunner) -> str:
    res = run(["gh", "api", "user", "--jq", ".login"])
    if res.returncode == 127:
        raise BackupError(
            "GitHub CLI 'gh' not found. Install it and run 'gh auth login', "
            "or pass --remote <git-url> to back up anywhere."
        )
    if not res.ok or not res.stdout.strip():
        raise BackupError(
            "Not authenticated with GitHub. Run 'gh auth login' "
            "(or pass --remote <git-url>)."
        )
    return res.stdout.strip()


def _repo_exists(run: CommandRunner, slug: str) -> bool:
    return run(["gh", "repo", "view", slug]).ok


def _create_private_repo(run: CommandRunner, slug: str, *, private: bool) -> None:
    vis = "--private" if private else "--public"
    res = run(["gh", "repo", "create", slug, vis])
    if not res.ok:
        raise BackupError(f"Could not create {slug}: {res.stderr.strip() or res.stdout.strip()}")


def _git(run: CommandRunner, root: Path, *args: str) -> RunResult:
    return run(["git", *args], cwd=root)


def _ensure_identity(run: CommandRunner, root: Path) -> list[str]:
    """If the backup repo has no commit identity, supply a local one inline
    so the backup commit never fails on a fresh machine."""
    if _git(run, root, "config", "user.email").ok:
        return []
    return [
        "-c",
        "user.email=cophilo@localhost",
        "-c",
        "user.name=cophilo backup",
    ]


def backup_paths(cfg: Config) -> list[str]:
    """The paths under ``data/`` that are *built from your notes/articles* and
    therefore belong in the backup, as strings relative to ``data/``.

    The journals memory index and ``journals.yaml`` are deliberately absent —
    they derive from a source already tracked in the main repo. Only paths
    that currently exist are returned (git pathspecs must match)."""
    data = cfg.data_dir.resolve()
    candidates = [
        cfg.corpus_dir,
        cfg.normalized_dir,
        cfg.rendered_dir,
        cfg.data_dir / "proposals",
        cfg.syntheses_dir,
        cfg.db_path,  # the extraction DB; NOT memory_db_path
    ]
    out: list[str] = []
    for p in candidates:
        p = p.resolve()
        if not p.exists():
            continue
        try:
            out.append(p.relative_to(data).as_posix())
        except ValueError:
            continue  # outside data/ (e.g. a custom COPHILO_DB_PATH) — skip
    return out


def backup_corpus(
    cfg: Config,
    *,
    name: str = DEFAULT_REPO_NAME,
    private: bool = True,
    remote: str | None = None,
    message: str | None = None,
    runner: CommandRunner | None = None,
) -> BackupResult:
    """Create-if-missing the private backup repo and push the corpus plus
    everything derived from it."""
    run = runner or default_runner
    root = cfg.data_dir
    if root.resolve() == cfg.repo_root.resolve():
        raise BackupError("Refusing to back up the repo root.")
    cfg.corpus_dir.mkdir(parents=True, exist_ok=True)

    # 1. Resolve the backup remote.
    slug: str | None = None
    repo_created = False
    if remote:
        remote_url = remote
    else:
        owner = _gh_owner(run)
        slug = f"{owner}/{name}"
        if _repo_exists(run, slug):
            repo_created = False
        else:
            _create_private_repo(run, slug, private=private)
            repo_created = True
        remote_url = f"https://github.com/{slug}.git"

    # 2. Init data/ as its own repo on first run.
    initialised = False
    if not (root / ".git").exists():
        res = _git(run, root, "init", "-b", "main")
        if not res.ok:
            raise BackupError(f"git init failed: {res.stderr.strip()}")
        initialised = True

    # Marker/readme lives inside the corpus, which the main repo already
    # ignores — so it never pollutes the tool repo's `git status`.
    readme = cfg.corpus_dir / "README.md"
    if not readme.exists():
        readme.write_text(_BACKUP_README, encoding="utf-8")

    # 3. Point origin at the backup remote.
    if _git(run, root, "remote", "get-url", "origin").ok:
        _git(run, root, "remote", "set-url", "origin", remote_url)
    else:
        _git(run, root, "remote", "add", "origin", remote_url)

    # 4. Stage only the built-from-your-stuff paths (so siblings like
    #    memory.sqlite / journals.yaml are never pulled in), then commit
    #    the staged index. We do NOT pass the paths again to `commit`: an
    #    existing-but-empty derived dir (e.g. normalized/ before the first
    #    ingest) is a valid pathspec for `add` but matches nothing git
    #    knows, which would make `commit -- <that dir>` fail outright.
    paths = backup_paths(cfg)
    _git(run, root, "add", "-A", "--", *paths)
    dirty = bool(
        _git(run, root, "status", "--porcelain", "--", *paths).stdout.strip()
    )
    msg = message or f"cophilo backup {datetime.now(UTC).isoformat(timespec='seconds')}"
    committed = False
    if dirty:
        ident = _ensure_identity(run, root)
        res = _git(run, root, *ident, "commit", "-m", msg)
        if not res.ok:
            raise BackupError(f"git commit failed: {res.stderr.strip() or res.stdout.strip()}")
        committed = True

    # 5. Push.
    push = _git(run, root, "push", "-u", "origin", "main")
    if not push.ok:
        raise BackupError(
            f"git push failed: {push.stderr.strip() or push.stdout.strip()}"
        )

    return BackupResult(
        remote_url=remote_url,
        slug=slug,
        repo_created=repo_created,
        initialised=initialised,
        committed=committed,
        pushed=True,
        message=msg,
    )
