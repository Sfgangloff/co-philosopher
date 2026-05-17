#!/usr/bin/env bash
# First-time setup for a fresh clone / fork of co-philosopher.
#
#   Workflow:   1) ./setup.sh     2) ./cophilo   (or: cophilo, in the venv)
#
# Idempotent: safe to re-run. Creates a .venv, installs the package + dev
# deps, and initialises the data dir + database.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }

EXTRAS="${COPHILO_EXTRAS:-dev}"   # e.g. COPHILO_EXTRAS="dev,memory" ./setup.sh

if command -v uv >/dev/null 2>&1; then
  say "Creating virtualenv with uv"
  uv venv
  say "Installing cophilo (extras: ${EXTRAS})"
  uv pip install -e ".[${EXTRAS}]"
else
  warn "uv not found — falling back to python3 -m venv + pip (slower)."
  say "Creating virtualenv"
  python3 -m venv .venv
  ./.venv/bin/python -m pip install -q -U pip
  say "Installing cophilo (extras: ${EXTRAS})"
  ./.venv/bin/python -m pip install -e ".[${EXTRAS}]"
fi

say "Initialising data dir + database"
./.venv/bin/cophilo init

command -v pandoc >/dev/null 2>&1 || \
  warn "pandoc not found — needed for DOCX/LaTeX ingest (brew install pandoc)."
command -v gh >/dev/null 2>&1 || \
  warn "GitHub CLI 'gh' not found — needed for 'cophilo backup' (or use --remote)."

cat <<'DONE'

Setup complete. Next:

  ./cophilo            # start the terminal interface
  # or:  source .venv/bin/activate  &&  cophilo

Inside, type `help` for every command. Typical flow:
  cophilo dialog  →  cophilo ingest  →  cophilo propose  →  cophilo draft
Back everything up anytime with:  cophilo backup
DONE
