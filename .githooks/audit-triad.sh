#!/usr/bin/env bash
# Wide-sweep audit across the entire working tree. Run before publishing.
# Reports (does not block) every occurrence of triad-related identifiers,
# including lowercase forms that the pre-commit hook intentionally skips.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

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

# Exclude: this script itself, the pre-commit hook, .git, .venv, build artifacts.
git ls-files \
  | grep -vE '^(\.githooks/|dashboard/(node_modules|dist)/)' \
  | xargs grep -nE "\\b(${JOINED})\\b" 2>/dev/null \
  | grep -v '^[^:]*:[0-9]*:[[:space:]]*#.*echo' \
  || { echo "no triad identifiers found"; exit 0; }
