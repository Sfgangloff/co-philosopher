#!/usr/bin/env python3
"""Pre-Bash hook for Claude Code in autonomous mode.

Reads tool-call JSON on stdin; exits 2 with a stderr message to block.

The allow/deny lists in .claude/settings.json are the first line — this
script is the second line, catching variants the glob patterns can't
predict. Critically, it strips quoted strings and heredoc bodies BEFORE
pattern matching, so a commit-message body that *mentions* a denied
command (as documentation) does not get blocked — a real bug bit us.

Fail-closed: if anything goes wrong parsing the payload, exit non-zero.
"""
from __future__ import annotations

import json
import os
import re
import sys


def deny(reason: str) -> None:
    print(f"BLOCKED by .claude/hooks/pre-bash.sh: {reason}", file=sys.stderr)
    sys.exit(2)


def read_command() -> str:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return ""
    return ((payload.get("tool_input") or {}).get("command") or "")


# --- Strip inert content (quoted strings, heredoc bodies) ------------------

_HEREDOC_RE = re.compile(
    r"<<-?\s*['\"]?(?P<tag>\w+)['\"]?[^\n]*\n.*?^\s*\1\s*$",
    re.DOTALL | re.MULTILINE,
)
_SQUOTE_RE = re.compile(r"'(?:[^'\\]|\\.)*'", re.DOTALL)
_DQUOTE_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)


def strip_inert(s: str) -> str:
    s = _HEREDOC_RE.sub("<<HEREDOC>>", s)
    s = _SQUOTE_RE.sub("''", s)
    s = _DQUOTE_RE.sub('""', s)
    return s


def subcommands(stripped: str) -> list[str]:
    """Split into the parts the shell would treat as separate commands.

    Conservative: any of `;`, `&&`, `||`, `|`, `&`, newline, or a shell
    keyword that begins a new command (`then`, `else`, `fi`, `do`, `done`).
    """
    parts = re.split(
        r"[;|&\n]+|\b(?:then|else|elif|fi|do|done)\b",
        stripped,
    )
    return [p.strip() for p in parts if p and p.strip()]


# --- Pattern table ---------------------------------------------------------

DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|\s)sudo\b"), "sudo is forbidden"),
    (re.compile(r"\bgit\s+push\b.*?(\s|=)(--force|--force-with-lease|-f)\b"),
     "force-push is forbidden"),
    (re.compile(r"\bgit\s+reset\b.*?\s--hard\b"),
     "git reset --hard is forbidden (use git revert)"),
    (re.compile(r"\bgit\s+clean\b.*?\s-f"),
     "git clean -f is forbidden (use git stash)"),
    (re.compile(r"\bgit\s+\w+.*?\s--no-verify\b"),
     "skipping commit hooks is forbidden"),
    (re.compile(r"\bgit\s+commit\b.*?\s--amend\b"),
     "git --amend is forbidden in autonomous mode"),
    (re.compile(r"\bgit\s+rebase\b"),
     "git rebase requires explicit owner approval"),
    (re.compile(r"\bgit\s+config\b"),
     "git config changes require explicit owner approval"),
    (re.compile(r"\bgit\s+branch\s+-D\b"),
     "git branch -D (force-delete) is forbidden"),
    (re.compile(r"\bgit\s+checkout\s+--(\s|$)"),
     "git checkout -- (discard working changes) is forbidden"),
    (re.compile(r"\bgit\s+add\s+(-A|-u)\b"),
     "use explicit file lists, not git add -A/-u"),

    # Cost-bearing cophilo subcommands.
    (re.compile(r"\bcophilo\s+extract\b"),
     "cophilo extract calls Claude (costs money); ask the owner first"),
    (re.compile(r"\bcophilo\s+propose\b"),
     "cophilo propose calls Claude (costs money); ask the owner first"),
    (re.compile(r"\bcophilo\s+draft\b"),
     "cophilo draft calls Claude (costs money); ask the owner first"),
    (re.compile(r"\bcophilo\s+biblio\s+synthesize\b"),
     "cophilo biblio synthesize calls Claude (costs money); ask the owner first"),
    (re.compile(r"\bcophilo\s+dialog\b.*?\s--socratic\b"),
     "cophilo dialog --socratic calls Claude (costs money); ask the owner first"),

    # Shell redirection into private data/
    (re.compile(r">>?\s*data/(corpus|db|syntheses|normalized|rendered|proposals)/"),
     "shell write into private data/ dir is forbidden"),
]


def check_rm_rf(tokens: list[str]) -> None:
    """`rm` with both `r` and `f` in the first flag bundle."""
    if not tokens or tokens[0] != "rm":
        return
    for t in tokens[1:]:
        if t.startswith("--"):
            continue
        if t.startswith("-"):
            flags = t[1:]
            if "r" in flags and "f" in flags:
                deny("rm -rf is forbidden in autonomous mode")
            return  # only the first flag bundle matters
        return  # past flags, into paths


def check_git_add_dot(tokens: list[str]) -> None:
    """`git add .` — only when `.` is a literal arg (not a substring of a path)."""
    if len(tokens) >= 3 and tokens[0] == "git" and tokens[1] == "add":
        if any(t == "." for t in tokens[2:]):
            deny("git add . is forbidden; list files explicitly")


def check_git_push(sub: str) -> None:
    """Any `git push` requires `COPHILO_ALLOW_PUSH=1` in env."""
    if re.search(r"\bgit\s+push\b", sub):
        if os.environ.get("COPHILO_ALLOW_PUSH") != "1":
            deny(
                "git push requires COPHILO_ALLOW_PUSH=1 "
                "(owner authorisation per session)"
            )


def check_cophilo_review(sub: str) -> None:
    """`cophilo review <path>` calls Claude; `cophilo review <path> --clear`
    does not. Block only when no `--clear` flag appears anywhere in the args."""
    if re.search(r"\bcophilo\s+review\b", sub) and not re.search(r"\B--clear\b", sub):
        deny("cophilo review calls Claude (costs money); ask the owner first")


def main() -> None:
    cmd = read_command()
    if not cmd:
        return
    stripped = strip_inert(cmd)
    for sub in subcommands(stripped):
        for pat, reason in DENY_PATTERNS:
            if pat.search(sub):
                deny(reason)
        tokens = sub.split()
        check_rm_rf(tokens)
        check_git_add_dot(tokens)
        check_git_push(sub)
        check_cophilo_review(sub)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # fail-closed
        print(
            f"BLOCKED by .claude/hooks/pre-bash.sh: hook error: {e}",
            file=sys.stderr,
        )
        sys.exit(2)
