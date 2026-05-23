#!/usr/bin/env bash
# End-of-turn guard. Two jobs:
#   1) Fire a macOS notification so the owner sees the turn boundary.
#   2) If the working tree has touched src/ or tests/, run ruff + pytest;
#      print a loud warning (don't block) so Claude/owner sees regressions
#      before they get further from the change that caused them.
#
# We deliberately don't block Stop — a deadlock where Claude can't yield
# would be worse than a regression Claude tells the owner about.
set -uo pipefail

notify() {
  # AppleScript notification — silent if user has do-not-disturb on.
  # The second arg is the title, third is the message.
  local title="$1" msg="$2"
  /usr/bin/osascript -e "display notification \"$msg\" with title \"$title\"" 2>/dev/null || true
}

repo="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$repo" || exit 0

# Did this session touch source code? Cheap proxy: any unstaged or staged
# change under src/ or tests/.
touched_code=0
if git status --porcelain 2>/dev/null | grep -qE '^.{0,2} (src/|tests/)'; then
  touched_code=1
fi

ruff_status="skipped"
pytest_status="skipped"

if [[ "$touched_code" == "1" ]] && [[ -x .venv/bin/ruff ]]; then
  if .venv/bin/ruff check src tests >/tmp/cophilo-stop-ruff.log 2>&1; then
    ruff_status="ok"
  else
    ruff_status="FAILED"
    echo "[stop hook] ruff check FAILED:" >&2
    cat /tmp/cophilo-stop-ruff.log >&2
  fi
fi

if [[ "$touched_code" == "1" ]] && [[ -x .venv/bin/python ]]; then
  if .venv/bin/python -m pytest -q >/tmp/cophilo-stop-pytest.log 2>&1; then
    pytest_status="ok"
  else
    pytest_status="FAILED"
    echo "[stop hook] pytest FAILED:" >&2
    tail -30 /tmp/cophilo-stop-pytest.log >&2
  fi
fi

# Warn if anything new appeared under data/corpus/ (would mean Claude
# accidentally generated user-owned content). pre-write.sh should block
# this, but report at end-of-turn too as a backstop.
new_corpus=$(git status --porcelain data/corpus/ 2>/dev/null | wc -l | tr -d ' ')
if [[ "$new_corpus" != "0" ]]; then
  echo "[stop hook] WARNING: $new_corpus change(s) under data/corpus/ — should not happen" >&2
fi

# Notification summary.
case "$pytest_status:$ruff_status" in
  ok:ok)       notify "co-philosopher" "Turn done — tests pass" ;;
  FAILED:*)    notify "co-philosopher" "Turn done — TESTS FAILED" ;;
  ok:FAILED)   notify "co-philosopher" "Turn done — lint failed" ;;
  skipped:*)   notify "co-philosopher" "Turn done" ;;
esac

exit 0
