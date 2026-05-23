#!/usr/bin/env bash
# After a `git commit`, verify the new HEAD has the Co-Authored-By footer
# and a subject ≤70 chars. Doesn't block (the commit already happened),
# but prints a loud warning so the issue is visible this turn and can be
# fixed with a follow-up commit.
set -uo pipefail

payload="$(cat)"
cmd="$(printf '%s' "$payload" | /usr/bin/python3 -c '
import json, sys
try:
    obj = json.loads(sys.stdin.read())
    print(obj.get("tool_input", {}).get("command", ""))
except Exception:
    print("")
')"

# Only inspect git commit calls.
case "$cmd" in
  *"git commit"*) : ;;
  *) exit 0 ;;
esac

# Must be inside a repo for the rest to apply.
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

subject="$(git log -1 --pretty=%s 2>/dev/null || true)"
body="$(git log -1 --pretty=%B 2>/dev/null || true)"

warn() {
  echo "[post-bash warn] $1" >&2
}

if (( ${#subject} > 70 )); then
  warn "commit subject is ${#subject} chars (>70): $subject"
fi

if ! grep -q '^Co-Authored-By:' <<< "$body"; then
  warn "commit missing Co-Authored-By footer; add it in the next commit"
fi

exit 0
