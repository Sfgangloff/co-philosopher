"""Anthropic SDK wrapper for cophilo extraction passes.

Uses ``client.messages.parse()`` for typed structured outputs and top-level
``cache_control`` so the (large, stable) system prompt + taxonomy snapshot
is reused across documents within the cache TTL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

import anthropic
from pydantic import BaseModel

from cophilo.config import Config

T = TypeVar("T", bound=BaseModel)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class ExtractionResult:
    parsed: BaseModel
    cache_read_tokens: int
    cache_write_tokens: int
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    """Subset of the Anthropic client surface we use.

    Defined as a Protocol so tests can inject a fake without depending on
    the full SDK.
    """

    def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        response_model: type[T],
        max_tokens: int,
    ) -> ExtractionResult: ...


class AnthropicClient:
    """Thin wrapper around ``anthropic.Anthropic`` that uses ``messages.parse()``
    with prompt caching."""

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)

    def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        response_model: type[T],
        max_tokens: int = 8000,
    ) -> ExtractionResult:
        response = self._client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user}],
            output_format=response_model,
        )
        usage: Any = response.usage
        return ExtractionResult(
            parsed=response.parsed_output,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )


def make_client(cfg: Config) -> LLMClient:
    if not cfg.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env to run extraction passes."
        )
    return AnthropicClient(cfg.anthropic_api_key)


def load_prompt_template(language: str, pass_name: str) -> str:
    """Load a prompt template for the given language ('en' | 'fr') and pass.

    Falls back to English if a translation is missing.
    """
    candidate = _PROMPTS_DIR / language / f"{pass_name}.md"
    if not candidate.exists():
        candidate = _PROMPTS_DIR / "en" / f"{pass_name}.md"
    return candidate.read_text(encoding="utf-8")
