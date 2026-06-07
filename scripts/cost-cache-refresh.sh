#!/usr/bin/env bash
# cost-cache-refresh.sh — cumulative cross-session Claude Code cost backend.
#
# Scans every transcript JSONL under ~/.claude/projects/**/*.jsonl, tallies
# token usage per model, applies a pricing table, and writes day/week/month/total
# USD figures to ~/.claude/.cost-cache.json (atomically). Intended to run as a
# BACKGROUND refresher (not per-render). The live statusline reader consumes the
# JSON; this script never touches the statusline itself.
#
# Cost logic lives in cost_cache_computer.py (same directory).
#
# Fail-safe: the Python builds the full result in memory and only writes (temp +
# os.replace) on complete success. Any error leaves the previous cache intact.
set -euo pipefail

PYTHON="${HOME}/.claude/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 2>/dev/null || true)"
if [[ -z "$PYTHON" ]]; then
    echo "cost-cache-refresh: no python interpreter found" >&2
    exit 1
fi

exec "$PYTHON" "$HOME/.claude/scripts/cost_cache_computer.py" "$@"
