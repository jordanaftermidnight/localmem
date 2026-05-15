#!/usr/bin/env bash
# Wide-sweep audit across the entire working tree. Run before publishing
# and in CI to gate every PR.
#
# Exit codes:
#   0 — clean (no triad/upstream identifiers found)
#   1 — violations found (CI should fail on this)
#   2 — script error (git not available, etc.)
#
# Skipped: this hooks directory (intentional patterns), .git, .venv,
# node_modules, dashboard build output.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 2

PATTERNS=(
  'IRIS'
  'Iris'
  'iris'
  'SORIEL'
  'Soriel'
  'soriel'
  'ECHO'
  'Echo'
  'MNEMOSYNE'
  'Mnemosyne'
  'mnemosyne'
)

JOINED="$(IFS='|'; echo "${PATTERNS[*]}")"

# Collect candidate matches first, then filter false positives, then decide.
# `set -e` plus the explicit `|| true` keeps a "no matches" exit (1) from
# tearing down the script before we can interpret it.
raw_hits="$(
  git ls-files \
    | grep -vE '^(\.githooks/|dashboard/(node_modules|dist)/)' \
    | xargs grep -nE "\\b(${JOINED})\\b" 2>/dev/null \
    || true
)"

# Drop comment lines that just say "echo ..." (shell builtin, not an agent).
hits="$(
  echo "${raw_hits}" \
    | grep -vE '^[^:]*:[0-9]+:[[:space:]]*#.*echo' \
    || true
)"

if [[ -z "${hits//[[:space:]]/}" ]]; then
  echo "no triad identifiers found"
  exit 0
fi

echo "audit-triad: violations found"
echo ""
echo "${hits}"
echo ""
echo "Run with LOCALMEM_BYPASS_TRIAD_SCAN=1 git commit ... to override locally,"
echo "but CI will still fail on these matches. Rewrite to generic naming first."
exit 1
