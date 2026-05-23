#!/usr/bin/env bash
# Block Edit/Write/NotebookEdit calls targeting:
#   - paths outside the repo root
#   - private data/ subdirs (corpus, db, syntheses, normalized, rendered, proposals)
#   - secrets (.env*)
#   - the rails themselves (.claude/settings.json, .claude/hooks/**, .githooks/**)
# Fail-closed on any error.
set -euo pipefail

payload="$(cat)"
target="$(printf '%s' "$payload" | /usr/bin/python3 -c '
import json, sys
try:
    obj = json.loads(sys.stdin.read())
    inp = obj.get("tool_input", {})
    print(inp.get("file_path") or inp.get("notebook_path") or "")
except Exception:
    print("")
')"

deny() {
  echo "BLOCKED by .claude/hooks/pre-write.sh: $1" >&2
  exit 2
}

[[ -z "$target" ]] && exit 0  # nothing to check

# Resolve absolute path, even if the target doesn't exist yet.
abs="$(/usr/bin/python3 -c "import os,sys; print(os.path.abspath(sys.argv[1]))" "$target")"
repo="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
repo_abs="$(/usr/bin/python3 -c "import os,sys; print(os.path.abspath(sys.argv[1]))" "$repo")"

# Outside the repo root: refuse.
case "$abs" in
  "$repo_abs"|"$repo_abs"/*) : ;;
  *) deny "writes outside the project root are forbidden: $abs" ;;
esac

rel="${abs#"$repo_abs"/}"

# Private data — never written by Claude.
case "$rel" in
  data/corpus/*|data/db/*|data/syntheses/*|data/normalized/*|data/rendered/*|data/proposals/*)
    deny "writes to private data/ subdir are forbidden: $rel" ;;
esac

# Secrets.
case "$rel" in
  .env|.env.local|.env.*) deny "writing to env files is forbidden: $rel" ;;
esac

# Self-modification of rails.
case "$rel" in
  .claude/settings.json|.claude/hooks/*|.githooks/*)
    deny "Claude cannot edit its own rails ($rel) — flag the issue in your reply instead" ;;
esac

# .git/ internals.
case "$rel" in
  .git/*) deny "direct writes to .git/ are forbidden; use the git CLI" ;;
esac

exit 0
