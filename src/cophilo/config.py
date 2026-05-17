"""Runtime configuration for cophilo.

Resolves paths and API keys from environment variables (loaded from .env if
present). All paths are anchored on the repo root unless overridden by
COPHILO_DATA_DIR / COPHILO_DB_PATH.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _repo_root() -> Path:
    # src/cophilo/config.py → repo root is two parents up from src/cophilo
    return Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Config:
    repo_root: Path
    data_dir: Path
    corpus_dir: Path
    corpus_notes_dir: Path
    corpus_articles_dir: Path
    corpus_drafts_dir: Path
    normalized_dir: Path
    rendered_dir: Path
    db_path: Path
    memory_db_path: Path
    memory_embedding_model: str
    default_language: str
    anthropic_api_key: str | None
    openai_api_key: str | None
    claude_model_routine: str
    claude_model_hard: str
    embedding_model: str
    llm_backend: str
    claude_cli_path: str
    philarchive_base_url: str
    http_user_agent: str


@lru_cache(maxsize=1)
def get_config() -> Config:
    load_dotenv(_repo_root() / ".env", override=False)

    repo_root = _repo_root()
    data_dir = Path(os.environ.get("COPHILO_DATA_DIR", repo_root / "data")).resolve()
    db_path = Path(os.environ.get("COPHILO_DB_PATH", data_dir / "db" / "cophilo.sqlite")).resolve()
    memory_db_path = Path(
        os.environ.get("COPHILO_MEMORY_DB_PATH", data_dir / "db" / "memory.sqlite")
    ).resolve()

    return Config(
        repo_root=repo_root,
        data_dir=data_dir,
        corpus_dir=data_dir / "corpus",
        corpus_notes_dir=data_dir / "corpus" / "notes",
        corpus_articles_dir=data_dir / "corpus" / "articles",
        corpus_drafts_dir=data_dir / "corpus" / "drafts",
        normalized_dir=data_dir / "normalized",
        rendered_dir=data_dir / "rendered",
        db_path=db_path,
        memory_db_path=memory_db_path,
        memory_embedding_model=os.environ.get(
            "COPHILO_MEMORY_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"
        ),
        default_language=os.environ.get("COPHILO_DEFAULT_LANGUAGE", "en"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        claude_model_routine=os.environ.get("COPHILO_CLAUDE_MODEL_ROUTINE", "claude-sonnet-4-6"),
        claude_model_hard=os.environ.get("COPHILO_CLAUDE_MODEL_HARD", "claude-opus-4-7"),
        embedding_model=os.environ.get("COPHILO_EMBEDDING_MODEL", "text-embedding-3-large"),
        llm_backend=os.environ.get("COPHILO_LLM_BACKEND", "cli").strip().lower(),
        claude_cli_path=os.environ.get("COPHILO_CLAUDE_CLI", "claude"),
        philarchive_base_url=os.environ.get(
            "COPHILO_PHILARCHIVE_BASE_URL", "https://philarchive.org"
        ).rstrip("/"),
        http_user_agent=os.environ.get(
            "COPHILO_HTTP_USER_AGENT",
            "cophilo/0.1 (+https://github.com/Sfgangloff/co-philosopher)",
        ),
    )


def ensure_dirs(cfg: Config) -> None:
    for p in (
        cfg.data_dir,
        cfg.corpus_dir,
        cfg.corpus_notes_dir,
        cfg.corpus_articles_dir,
        cfg.corpus_drafts_dir,
        cfg.normalized_dir,
        cfg.rendered_dir,
        cfg.db_path.parent,
    ):
        p.mkdir(parents=True, exist_ok=True)
