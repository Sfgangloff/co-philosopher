# How to run Claude Code on this project

Quick reference for the owner. The detailed architecture is in
`.claude/CLAUDE.md` (rules), `.claude/settings.json` (permissions),
`.claude/hooks/*.sh` (runtime guards), and `.githooks/*` (git-side
floor).

## First time on a fresh clone

```bash
git config core.hooksPath .githooks
```

That's it. Everything else is tracked.

## Day-to-day launch

```bash
./bin/cc                 # interactive session
./bin/cc "<prompt>"      # one-shot
```

`bin/cc` runs `claude --permission-mode acceptEdits --strict-mcp-config`
from the repo root, so the project rails auto-load and only the project's
MCP servers (`cophilo-memory`) are exposed.

In `acceptEdits` mode:
- File edits **don't prompt** — the `pre-write.sh` hook polices them.
- Bash **doesn't prompt for allowlisted commands** (read-only inspection,
  `.venv/bin/python ...`, safe `git ...`, free `cophilo` subcommands,
  `git commit -m ...`, `git commit -F ...`).
- Bash **does prompt** for anything not on the allowlist.
- Bash is **always denied** for the deny-listed patterns (no prompt).

## To authorise a push (per session)

```bash
COPHILO_ALLOW_PUSH=1 ./bin/cc
```

The `pre-bash.sh` hook *and* `.githooks/pre-push` both require this env
var. Without it, every `git push` is refused. Force-push is always
refused, full stop.

For a one-off push from outside Claude:

```bash
COPHILO_ALLOW_PUSH=1 git push origin main
```

## What Claude can do without bothering you

- Read anything in the repo (except `data/` — it shouldn't read your notes anyway)
- Edit any source file (`src/`, `tests/`, `README.md`, etc.)
- Run `pytest`, `ruff`, the free `cophilo` subcommands (`list`, `concepts`,
  `questions`, `graph`, `help`)
- `git add <explicit files>`, `git commit -m "..."`, `git commit -F file`
- All read-only git ops (`status`, `diff`, `log`, `show`, `blame`)

## What Claude *cannot* do, ever

- Write into `data/corpus/`, `data/db/`, `data/syntheses/`,
  `data/normalized/`, `data/rendered/`, `data/proposals/`
- Write `.env`, `.env.local`, anything in `.git/`
- Edit its own rails (`.claude/settings.json`, `.claude/hooks/*`, `.githooks/*`)
- Write outside the project root
- Force-push, `--amend`, `--no-verify`, hard-reset, hard-clean, `git rebase`,
  `git config`, `git branch -D`, `git checkout --`, `git add -A/-u/.`
- Run `sudo`, `curl | sh`, `pip install`
- Run any cost-bearing `cophilo` subcommand (`extract`, `propose`, `draft`,
  `biblio synthesize`, `review <path>`, `dialog --socratic`) — these
  require you to invoke them yourself

## What happens at end-of-turn

`stop.sh` (in `.claude/hooks/`) fires:
- A macOS notification ("co-philosopher: turn done — tests pass" /
  "TESTS FAILED" / "lint failed")
- If `src/` or `tests/` were modified: re-runs `ruff check` + full
  `pytest -q` and prints failures to stderr so the next turn sees them
- Warns if anything new appeared under `data/corpus/` (shouldn't happen
  given `pre-write.sh`, but it's a backstop)

## If a hook breaks something

The rails fail-closed: if a hook crashes, the tool call is denied.
Symptom: every command starts getting blocked with weird messages.

To bypass for one session **only**:

```bash
mv .claude/settings.json /tmp/settings.json.bak
mv .claude/hooks /tmp/hooks.bak
claude                      # plain launch — no rails
# … fix the hook …
mv /tmp/hooks.bak .claude/hooks
mv /tmp/settings.json.bak .claude/settings.json
```

(The Claude-side block on editing the hooks themselves is the
intentional second floor; you, the owner, can always edit them outside
Claude.)

## Per-machine overrides

`.claude/settings.local.json` is gitignored and merges over
`settings.json`. Use it for genuinely per-machine things (e.g.
allowlisting a different python path). Do **not** put loosened security
rules there — those belong in the shared `settings.json`, reviewed.
