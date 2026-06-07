#!/bin/bash
# Hook: env-inject  — wire to SessionStart.
# Purpose: inject offline-mode env vars into the session environment so all
#          subsequent hooks and tools see them without manual prefixing.
#
# Mechanism: CC sets $CLAUDE_ENV_FILE (a file path) in the hook environment.
#            Appending plain KEY=value lines (NOT `export KEY=value`) to that
#            file causes CC to inject them as session-wide env vars.
#
# Rationale: HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 make superclaude's
#            memory/embedding tools run offline by default so agents no longer
#            need to prefix HF_HUB_OFFLINE=1 manually.
#
# FAIL-SAFE CONTRACT: set -uo pipefail (NOT set -e). Every path exits 0.
#   Never prints to stdout (would be parsed as hook JSON). Warnings to stderr.

set -uo pipefail

INPUT=$(cat 2>/dev/null || true)

# --- Guard: CLAUDE_ENV_FILE must be non-empty and its parent dir writable ----
if [ -z "${CLAUDE_ENV_FILE:-}" ]; then
    exit 0
fi

ENV_DIR=$(dirname "$CLAUDE_ENV_FILE")
if [ ! -w "$ENV_DIR" ]; then
    echo "env-inject: parent dir of CLAUDE_ENV_FILE not writable, skipping" >&2
    exit 0
fi

# --- Idempotent append: grep-guard each line before appending ----------------
if ! grep -qxF "HF_HUB_OFFLINE=1" "$CLAUDE_ENV_FILE" 2>/dev/null; then
    printf 'HF_HUB_OFFLINE=1\n' >> "$CLAUDE_ENV_FILE" || true
fi

if ! grep -qxF "TRANSFORMERS_OFFLINE=1" "$CLAUDE_ENV_FILE" 2>/dev/null; then
    printf 'TRANSFORMERS_OFFLINE=1\n' >> "$CLAUDE_ENV_FILE" || true
fi

exit 0
