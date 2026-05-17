"""LLM wrapper for cophilo's Claude calls.

Two interchangeable backends implement the same :class:`LLMClient` Protocol:

* :class:`AnthropicClient` — the Anthropic SDK via ``messages.parse()`` with
  prompt caching. Needs ``ANTHROPIC_API_KEY``.
* :class:`ClaudeCodeCLIClient` — shells out to the ``claude`` Code CLI in
  print mode. **No API key**: it reuses whatever auth the local Claude Code
  install already has. Structured output is requested as raw JSON and
  validated against the same pydantic models.

``make_client`` picks the backend from config (default: the CLI, so the tool
works key-free out of the box).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

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


class CLIRunner(Protocol):
    """Runs the ``claude`` CLI. Injected so tests need no real process."""

    def __call__(self, args: list[str], stdin: str) -> str: ...


def _default_cli_runner(args: list[str], stdin: str) -> str:
    proc = subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json_object(text: str) -> Any:
    """Pull a single JSON object out of a model's free-text reply.

    Handles bare JSON, ```json fenced blocks, and JSON embedded in prose.
    """
    text = text.strip()
    m = _JSON_FENCE_RE.search(text)
    candidate = m.group(1).strip() if m else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end > start:
            return json.loads(candidate[start : end + 1])
        raise


_CLI_SYSTEM = (
    "You are a precise JSON generation service. Obey the user's instructions "
    "and the supplied JSON Schema exactly. Output only one raw JSON object — "
    "no prose, no explanation, no markdown code fences."
)


class ClaudeCodeCLIClient:
    """LLMClient backed by the ``claude`` Code CLI (``claude -p``).

    Key-free: relies on the local Claude Code installation's own auth.
    """

    def __init__(self, cli_path: str = "claude", runner: CLIRunner | None = None) -> None:
        self._cli = cli_path
        self._run = runner or _default_cli_runner

    def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        response_model: type[T],
        max_tokens: int = 8000,
    ) -> ExtractionResult:
        schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        prompt = (
            f"{system}\n\n{user}\n\n"
            "Return ONLY a single JSON object that validates against this JSON "
            f"Schema (no markdown fences, no commentary):\n\n{schema}"
        )
        args = [
            self._cli,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--system-prompt",
            _CLI_SYSTEM,
            "--max-turns",
            "1",
        ]
        stdout = self._run(args, prompt)
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"claude CLI did not return JSON envelope: {stdout[:500]!r}"
            ) from e
        if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
            raise RuntimeError(f"claude CLI reported an error: {envelope.get('result', envelope)!r}")

        text = envelope.get("result", "")
        try:
            obj = _extract_json_object(text)
            parsed = response_model.model_validate(obj)
        except (json.JSONDecodeError, ValidationError) as e:
            raise RuntimeError(
                f"Could not parse {response_model.__name__} from CLI reply: {text[:500]!r}"
            ) from e

        usage: dict[str, Any] = envelope.get("usage") or {}
        return ExtractionResult(
            parsed=parsed,
            cache_read_tokens=usage.get("cache_read_input_tokens", 0) or 0,
            cache_write_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
            input_tokens=usage.get("input_tokens", 0) or 0,
            output_tokens=usage.get("output_tokens", 0) or 0,
        )


def make_client(cfg: Config) -> LLMClient:
    """Build the configured LLM backend.

    Default backend is the Claude Code CLI (no API key). Set
    ``COPHILO_LLM_BACKEND=api`` (with ``ANTHROPIC_API_KEY``) to use the SDK.
    """
    if cfg.llm_backend == "api":
        if not cfg.anthropic_api_key:
            raise RuntimeError(
                "COPHILO_LLM_BACKEND=api but ANTHROPIC_API_KEY is not set. "
                "Add the key, or use the default CLI backend (unset the variable)."
            )
        return AnthropicClient(cfg.anthropic_api_key)

    if shutil.which(cfg.claude_cli_path) is None:
        raise RuntimeError(
            f"Claude Code CLI '{cfg.claude_cli_path}' not found on PATH. "
            "Install Claude Code, or set COPHILO_LLM_BACKEND=api with ANTHROPIC_API_KEY."
        )
    return ClaudeCodeCLIClient(cfg.claude_cli_path)


def load_prompt_template(language: str, pass_name: str) -> str:
    """Load a prompt template for the given language ('en' | 'fr') and pass.

    Falls back to English if a translation is missing.
    """
    candidate = _PROMPTS_DIR / language / f"{pass_name}.md"
    if not candidate.exists():
        candidate = _PROMPTS_DIR / "en" / f"{pass_name}.md"
    return candidate.read_text(encoding="utf-8")
