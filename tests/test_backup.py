"""Tests for `cophilo backup`. The git/gh command runner is faked, so there
is no real git, no `gh`, and no network."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import cophilo.cli as cli_mod
from cophilo.backup import BackupError, BackupResult, RunResult, backup_corpus
from cophilo.cli import app
from cophilo.config import ensure_dirs, get_config


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("COPHILO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COPHILO_DB_PATH", str(data_dir / "db" / "cophilo.sqlite"))
    get_config.cache_clear()
    cfg = get_config()
    ensure_dirs(cfg)
    yield cfg
    get_config.cache_clear()


class FakeRunner:
    def __init__(self, *, owner="alice", repo_exists=False, dirty=True, gh=True, identity=True):
        self.calls: list[list[str]] = []
        self.owner = owner
        self.repo_exists = repo_exists
        self.dirty = dirty
        self.gh = gh
        self.identity = identity

    def __call__(self, args, *, cwd=None):
        self.calls.append(args)
        if args[0] == "gh":
            if not self.gh:
                return RunResult(127, "", "gh: command not found")
            if args[1:3] == ["api", "user"]:
                return RunResult(0, self.owner + "\n")
            if args[1:3] == ["repo", "view"]:
                return RunResult(0 if self.repo_exists else 1)
            return RunResult(0)  # repo create, etc.
        if args[0] == "git":
            i = 1
            while i < len(args) and args[i] == "-c":
                i += 2
            sub = args[i] if i < len(args) else ""
            if sub == "remote" and len(args) > i + 1 and args[i + 1] == "get-url":
                return RunResult(1)  # no origin yet
            if sub == "config":
                return RunResult(0 if self.identity else 1)
            if sub == "status":
                return RunResult(0, "M note.md\n" if self.dirty else "")
            return RunResult(0)
        return RunResult(0)

    def find(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def test_creates_private_repo_when_missing(isolated_data_dir):
    cfg = isolated_data_dir
    r = FakeRunner(owner="alice", repo_exists=False)
    result = backup_corpus(cfg, runner=r)

    assert result.repo_created is True
    assert result.slug == "alice/co-philosopher-backup"
    assert result.remote_url == "https://github.com/alice/co-philosopher-backup.git"
    assert result.initialised and result.committed and result.pushed

    create = r.find("gh", "repo", "create")
    assert create and create[0] == [
        "gh", "repo", "create", "alice/co-philosopher-backup", "--private",
    ]
    assert r.find("git", "init", "-b", "main")
    assert r.find("git", "push", "-u", "origin", "main")
    readme = cfg.corpus_dir / "README.md"
    assert readme.exists() and "private" in readme.read_text()


def test_skips_create_when_repo_exists(isolated_data_dir):
    r = FakeRunner(repo_exists=True)
    result = backup_corpus(isolated_data_dir, runner=r)
    assert result.repo_created is False
    assert r.find("gh", "repo", "create") == []


def test_remote_override_bypasses_gh(isolated_data_dir):
    r = FakeRunner()
    result = backup_corpus(
        isolated_data_dir, remote="git@example.org:me/mirror.git", runner=r
    )
    assert result.remote_url == "git@example.org:me/mirror.git"
    assert result.slug is None
    assert [c for c in r.calls if c[0] == "gh"] == []  # gh never touched


def test_missing_gh_raises_actionable_error(isolated_data_dir):
    r = FakeRunner(gh=False)
    with pytest.raises(BackupError, match="gh"):
        backup_corpus(isolated_data_dir, runner=r)


def test_nothing_to_commit_still_pushes(isolated_data_dir):
    cfg = isolated_data_dir
    (cfg.data_dir / ".git").mkdir()  # backup repo already initialised
    r = FakeRunner(repo_exists=True, dirty=False)
    result = backup_corpus(cfg, runner=r)
    assert result.initialised is False
    assert result.committed is False
    assert result.pushed is True
    assert [c for c in r.calls if "commit" in c] == []


def test_backup_stages_corpus_and_derived_folders(isolated_data_dir):
    cfg = isolated_data_dir
    (cfg.db_path).write_text("", encoding="utf-8")  # extraction DB exists
    r = FakeRunner()
    backup_corpus(cfg, runner=r)

    add = r.find("git", "add", "-A", "--")
    assert add, r.calls
    staged = set(add[0][4:])
    assert {"corpus", "normalized", "rendered", "db/cophilo.sqlite"} <= staged
    # the journals memory index is never staged
    assert "db/memory.sqlite" not in staged


def test_commit_takes_no_pathspec(isolated_data_dir):
    """Regression: an existing-but-empty derived dir (e.g. normalized/
    before the first ingest) is a valid `add` pathspec but matches
    nothing git knows, so `commit -- <dir>` used to fail outright. The
    commit must commit the staged index, with no pathspec."""
    cfg = isolated_data_dir
    cfg.normalized_dir.mkdir(parents=True, exist_ok=True)  # exists, empty
    cfg.rendered_dir.mkdir(parents=True, exist_ok=True)
    r = FakeRunner()
    backup_corpus(cfg, runner=r)

    commits = [c for c in r.calls if "commit" in c]
    assert commits, r.calls
    commit = commits[0]
    assert "--" not in commit[commit.index("commit") :]
    # staging is still scoped to the backup paths
    add = r.find("git", "add", "-A", "--")
    assert add and {"normalized", "rendered"} <= set(add[0][4:])


def test_inline_identity_when_git_unconfigured(isolated_data_dir):
    r = FakeRunner(identity=False)
    backup_corpus(isolated_data_dir, runner=r)
    commits = [c for c in r.calls if "commit" in c]
    assert commits
    assert "user.email=cophilo@localhost" in commits[0]


def test_push_failure_surfaces(isolated_data_dir):
    class PushFails(FakeRunner):
        def __call__(self, args, *, cwd=None):
            if args[0] == "git" and "push" in args:
                self.calls.append(args)
                return RunResult(1, "", "remote rejected")
            return super().__call__(args, cwd=cwd)

    with pytest.raises(BackupError, match="push failed"):
        backup_corpus(isolated_data_dir, runner=PushFails())


# --- CLI -----------------------------------------------------------------

runner = CliRunner()


def test_cli_backup_ok(monkeypatch, isolated_data_dir):
    def fake_backup(cfg, **kw):
        return BackupResult(
            remote_url="https://github.com/alice/co-philosopher-backup.git",
            slug="alice/co-philosopher-backup",
            repo_created=True,
            initialised=True,
            committed=True,
            pushed=True,
            message="cophilo backup",
        )

    monkeypatch.setattr(cli_mod, "backup_corpus", fake_backup)
    res = runner.invoke(app, ["backup"])
    assert res.exit_code == 0, res.output
    assert "Backup →" in res.output
    assert "created private repo alice/co-philosopher-backup" in res.output


def test_cli_backup_error_exit(monkeypatch, isolated_data_dir):
    def boom(cfg, **kw):
        raise BackupError("Run 'gh auth login'")

    monkeypatch.setattr(cli_mod, "backup_corpus", boom)
    res = runner.invoke(app, ["backup"])
    assert res.exit_code == 1
    assert "gh auth login" in res.output
