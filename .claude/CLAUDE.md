# co-philosopher — rules for autonomous Claude Code

You are working inside the **co-philosopher** repo. The owner runs you
unattended for long stretches. Follow these rules without prompting; the
hooks in `.claude/hooks/` and `.githooks/` enforce most of them and will
block tool calls that violate them.

## Identity of this repo

This repo is the **tool** (a Typer CLI app, `cophilo`). The owner's actual
philosophical work — notes, articles, drafts, the extraction database —
lives under `data/` and is private. The data policy is in `.gitignore`:
`data/corpus/`, `data/normalized/`, `data/rendered/`, `data/syntheses/`,
`data/proposals/`, `data/db/*.sqlite` are all gitignored and personal.

Treat `data/` as untouchable. The hooks will block writes to it.

## Files you must never touch

- `data/corpus/**`, `data/normalized/**`, `data/rendered/**`,
  `data/syntheses/**`, `data/proposals/**`, `data/db/**`
- `.env`, `.env.local`
- `.git/**` — use the `git` CLI, never write to refs/objects directly
- `.claude/settings.json`, `.claude/hooks/**`, `.githooks/**` —
  you cannot edit your own rails. If a rail is wrong, surface the issue
  in your reply and let the owner change it.

## LLM-billing rules

These cophilo subcommands call the Anthropic API and **cost money**:
`cophilo extract`, `cophilo propose`, `cophilo draft`, `cophilo biblio
synthesize`, `cophilo review`, `cophilo dialog --socratic`. Hooks
**deny** them. Never invoke them speculatively; if a task genuinely
requires running one, ask the owner first and explain why.

The non-billing read-only inspection commands (`cophilo list`,
`cophilo concepts`, `cophilo questions`, `cophilo graph`, `cophilo help`)
are free and allowlisted.

## Commit discipline

- Run `.venv/bin/python -m pytest -q` before committing. Don't commit on red.
- Stage explicit file lists. Avoid `git add -A` / `git add .` / `git add -u`
  on a working tree you haven't fully inspected.
- Commit messages: one-line subject (≤70 chars), blank line, body
  explaining the *why*, ending with
  `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- The `.githooks/commit-msg` hook enforces the footer and subject length.
- The `.githooks/pre-commit` hook re-runs ruff + pytest. It will reject
  the commit on failure — fix the issue and create a NEW commit, never
  bypass with `--no-verify`.

## Push policy

**Never push without explicit instruction from the owner.** "Push" must
be in their message. The `.githooks/pre-push` hook denies pushes unless
`COPHILO_ALLOW_PUSH=1` is in the environment.

Force-push is forbidden full stop. Don't even propose it.

## When to ask before acting

Always confirm with the owner before:
- Running any cost-bearing cophilo subcommand (see above)
- Any destructive git op: `reset --hard`, `branch -D`, `clean -fd`,
  `checkout --`, `rebase`, `amend`
- Installing, removing, or upgrading dependencies; touching pyproject.toml deps
- Adding or modifying MCP servers
- Pushing, opening a PR, deleting branches
- Touching `.gitignore` data-policy section (data dirs)

## Reporting findings — notify the owner

When the turn ends with something the owner should see — test failures,
blocked tool calls, a completed multi-step task, a discovery worth
flagging — call the `PushNotification` tool with a one-line summary.
**Do not call it for routine progress** (a passing test, a small edit).
Reserve it for: completion of a task longer than a single edit, errors
that need attention, anything you would want a coworker to ping you about.

The `stop.sh` hook ALSO sends a passive turn-end notification — you don't
need to duplicate it for trivial turns.

## Defaults

- Tests: `.venv/bin/python -m pytest -q`. Test files in `tests/`.
- Lint: `.venv/bin/ruff check src tests`. Format: `ruff format`.
- Prefer `Edit` over `Write`. Prefer modifying existing files over creating new ones.
- Don't add comments unless the *why* isn't obvious from the code.
- Don't create docs / planning files / status reports unless the owner asks.
- If a hook blocks you, **don't try to circumvent it** — relay the block
  to the owner and explain what you were attempting.

## If anything here conflicts with global instructions

Project rules win — that's why this file exists.
