#!/usr/bin/env bash
# Second-line defence for Bash tool calls. Receives tool-call JSON on stdin;
# exit 2 to block (stderr is shown back to Claude).
#
# The allow/deny lists in .claude/settings.json are the first line. This
# catches sneaky variants the deny patterns can't predict (`bash -c '…'`,
# env-var smuggling, paths outside the repo). Fail-closed on any error.
set -euo pipefail

payload="$(cat)"
cmd="$(printf '%s' "$payload" | /usr/bin/python3 -c '
import json, sys
try:
    obj = json.loads(sys.stdin.read())
    print(obj.get("tool_input", {}).get("command", ""))
except Exception:
    print("")
')"

deny() {
  echo "BLOCKED by .claude/hooks/pre-bash.sh: $1" >&2
  exit 2
}

trimmed="$(printf '%s' "$cmd" | sed -e 's/^[[:space:]]*//')"

# Hard floors no allowlist can override.
case "$trimmed" in
  *"rm -rf"*|*"rm -fr"*)                 deny "rm -rf is forbidden in autonomous mode" ;;
  *" sudo "*|"sudo "*)                   deny "sudo is forbidden" ;;
  *"git push --force"*|*"git push -f"*)  deny "force-push is forbidden" ;;
  *"git reset --hard"*)                  deny "git reset --hard is forbidden (use git revert)" ;;
  *"git clean -f"*|*"git clean -fd"*)    deny "git clean -f is forbidden (use git stash)" ;;
  *"--no-verify"*)                       deny "skipping commit hooks is forbidden" ;;
  *"--amend"*)                           deny "git --amend is forbidden in autonomous mode" ;;
  *"git rebase"*)                        deny "git rebase requires explicit owner approval" ;;
  *"git config"*)                        deny "git config changes require explicit owner approval" ;;
  *"git branch -D"*)                     deny "git branch -D (force-delete) is forbidden" ;;
  *"git checkout -- "*)                  deny "git checkout -- (discard working changes) is forbidden" ;;
  *"git add -A"*|*"git add -u"*)         deny "use explicit file lists, not git add -A/-u" ;;
esac

# `git add .` — allow only when the path arg is literally ".", not a
# substring. Cheap parse: split on whitespace.
read -r -a words <<< "$trimmed"
if [[ "${words[0]:-}" == "git" && "${words[1]:-}" == "add" ]]; then
  for arg in "${words[@]:2}"; do
    [[ "$arg" == "." ]] && deny "git add . is forbidden; list files explicitly"
  done
fi

# Push policy: deny unless COPHILO_ALLOW_PUSH=1 is in process env (the
# owner sets it for the session when they explicitly authorise push).
case "$trimmed" in
  *"git push"*)
    if [[ "${COPHILO_ALLOW_PUSH:-0}" != "1" ]]; then
      deny "git push requires COPHILO_ALLOW_PUSH=1 (owner authorisation per session)"
    fi
    ;;
esac

# Cost-bearing cophilo subcommands. Belt-and-braces with the deny list.
case "$trimmed" in
  *"cophilo extract"*|*"cophilo propose"*|*"cophilo draft"*| \
  *"cophilo biblio synthesize"*|*"cophilo review "*| \
  *"cophilo dialog "*"--socratic"*)
    deny "this cophilo subcommand calls Claude (costs money); ask the owner first"
    ;;
esac

# Refuse writes to private data/ via shell redirection.
case "$trimmed" in
  *">"*"data/corpus/"*|*">>"*"data/corpus/"*| \
  *">"*"data/db/"*|*">>"*"data/db/"*| \
  *">"*"data/syntheses/"*|*">>"*"data/syntheses/"*| \
  *">"*"data/normalized/"*|*">>"*"data/normalized/"*| \
  *">"*"data/rendered/"*|*">>"*"data/rendered/"*)
    deny "shell write into private data/ dir is forbidden"
    ;;
esac

exit 0
