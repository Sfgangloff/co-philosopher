#!/usr/bin/env bash
# After a successful Edit/Write of a .py file, run ruff check (no --fix —
# don't quietly change code under Claude's feet). Print the summary so
# Claude sees lint issues and can address them this turn. Never blocks.
set -uo pipefail  # no -e: a ruff non-zero shouldn't break the hook

payload="$(cat)"
target="$(printf '%s' "$payload" | /usr/bin/python3 -c '
import json, sys
try:
    obj = json.loads(sys.stdin.read())
    inp = obj.get("tool_input", {})
    print(inp.get("file_path") or "")
except Exception:
    print("")
')"

[[ -z "$target" ]] && exit 0
[[ "$target" == *.py ]] || exit 0
[[ -x .venv/bin/ruff ]] || exit 0

# Quiet check; show output only if there are findings.
out="$(.venv/bin/ruff check "$target" 2>&1)" || true
if [[ -n "$out" ]] && ! grep -q '^All checks passed' <<< "$out"; then
  echo "[post-edit ruff] $target:" >&2
  echo "$out" >&2
fi

exit 0
