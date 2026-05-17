"""Tests for the key-free Claude Code CLI backend and backend selection.

No real ``claude`` process is spawned — the CLI runner is injected.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from cophilo.config import get_config
from cophilo.extract.claude import (
    AnthropicClient,
    ClaudeCodeCLIClient,
    _extract_json_object,
    make_client,
)


class Tiny(BaseModel):
    answer: str
    n: int


def _envelope(result_text: str, *, is_error: bool = False, usage: dict | None = None) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "error" if is_error else "success",
            "is_error": is_error,
            "result": result_text,
            "usage": usage
            or {
                "input_tokens": 120,
                "output_tokens": 30,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 5,
            },
        }
    )


class FakeRunner:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.args: list[str] | None = None
        self.stdin: str | None = None

    def __call__(self, args, stdin):
        self.args = args
        self.stdin = stdin
        return self.stdout


# --- json extraction -----------------------------------------------------


def test_extract_json_object_variants():
    obj = {"answer": "ok", "n": 1}
    assert _extract_json_object(json.dumps(obj)) == obj
    assert _extract_json_object(f"```json\n{json.dumps(obj)}\n```") == obj
    assert _extract_json_object(f"Here you go:\n{json.dumps(obj)}\nDone.") == obj


# --- CLI client ----------------------------------------------------------


def test_cli_client_parses_and_maps_usage():
    runner = FakeRunner(_envelope('{"answer": "yes", "n": 7}'))
    client = ClaudeCodeCLIClient(cli_path="claude", runner=runner)
    res = client.call(
        model="claude-opus-4-7",
        system="SYSTEM BLOCK",
        user="USER MESSAGE",
        response_model=Tiny,
    )
    assert isinstance(res.parsed, Tiny)
    assert res.parsed.answer == "yes" and res.parsed.n == 7
    assert (res.input_tokens, res.output_tokens) == (120, 30)
    assert (res.cache_read_tokens, res.cache_write_tokens) == (80, 5)

    # invocation shape: print mode, json output, model + clean system prompt
    assert runner.args[:4] == ["claude", "-p", "--output-format", "json"]
    assert "--model" in runner.args and "claude-opus-4-7" in runner.args
    assert "--system-prompt" in runner.args
    # the real instructions + schema travel in the piped prompt, not argv
    assert "SYSTEM BLOCK" in runner.stdin
    assert "USER MESSAGE" in runner.stdin
    assert '"properties"' in runner.stdin  # JSON Schema embedded


def test_cli_client_handles_fenced_json():
    runner = FakeRunner(_envelope('```json\n{"answer": "fenced", "n": 1}\n```'))
    client = ClaudeCodeCLIClient(runner=runner)
    res = client.call(model="m", system="s", user="u", response_model=Tiny)
    assert res.parsed.answer == "fenced"


def test_cli_client_raises_on_cli_error():
    runner = FakeRunner(_envelope("rate limited", is_error=True))
    client = ClaudeCodeCLIClient(runner=runner)
    with pytest.raises(RuntimeError, match="claude CLI reported an error"):
        client.call(model="m", system="s", user="u", response_model=Tiny)


def test_cli_client_raises_on_unparseable_result():
    runner = FakeRunner(_envelope("not json at all"))
    client = ClaudeCodeCLIClient(runner=runner)
    with pytest.raises(RuntimeError, match="Could not parse Tiny"):
        client.call(model="m", system="s", user="u", response_model=Tiny)


def test_cli_client_raises_on_non_envelope():
    runner = FakeRunner("<<not a json envelope>>")
    client = ClaudeCodeCLIClient(runner=runner)
    with pytest.raises(RuntimeError, match="did not return JSON envelope"):
        client.call(model="m", system="s", user="u", response_model=Tiny)


# --- backend selection ---------------------------------------------------


@pytest.fixture
def clean_config(monkeypatch):
    for k in ("COPHILO_LLM_BACKEND", "ANTHROPIC_API_KEY", "COPHILO_CLAUDE_CLI"):
        monkeypatch.delenv(k, raising=False)
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def test_make_client_defaults_to_cli(monkeypatch, clean_config):
    monkeypatch.setattr("cophilo.extract.claude.shutil.which", lambda _: "/usr/bin/claude")
    assert isinstance(make_client(get_config()), ClaudeCodeCLIClient)


def test_make_client_cli_missing_binary(monkeypatch, clean_config):
    monkeypatch.setattr("cophilo.extract.claude.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="not found on PATH"):
        make_client(get_config())


def test_make_client_api_requires_key(monkeypatch, clean_config):
    monkeypatch.setenv("COPHILO_LLM_BACKEND", "api")
    get_config.cache_clear()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
        make_client(get_config())


def test_make_client_api_with_key(monkeypatch, clean_config):
    monkeypatch.setenv("COPHILO_LLM_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    get_config.cache_clear()
    assert isinstance(make_client(get_config()), AnthropicClient)
