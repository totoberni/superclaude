#!/usr/bin/env bash
# Auto-archive stale orchs: scans meta-registry for "Decommissioned" / "Archived"
# entries whose comms/<orch>/ still exists, and moves them to comms/_archive/.
# Also archives matching agents/<orch>.md if present.
#
# Safe to run multiple times — idempotent (no-op when nothing to archive).
# Skips orchs whose session timer is still active (alive) and any orch passed
# via --exclude (typically the caller's own session).
#
# Usage:
#   auto-archive-stale-orchs.sh [--dry-run] [--exclude <orch-name>]
#
# Exit codes:
#   0  success (work done, or no work to do)
#   1  registry not found
#
# Audit ref: O12 (stale orchs auto-archive on SessionEnd).

set -uo pipefail

REGISTRY="$HOME/.claude/comms/meta-registry.md"
COMMS_DIR="$HOME/.claude/comms"
ARCHIVE_DIR="$COMMS_DIR/_archive"
AGENTS_DIR="$HOME/.claude/agents"
AGENT_ARCHIVE="$AGENTS_DIR/_archive"
TIMER_DIR="$HOME/.claude/session-timers"

dry_run=false
exclude_orch=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    --exclude) exclude_orch="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

[ -f "$REGISTRY" ] || { echo "Registry not found: $REGISTRY" >&2; exit 1; }
mkdir -p "$ARCHIVE_DIR" "$AGENT_ARCHIVE"

# Extract orch names from any markdown table under a "Decommissioned" or
# "Archived" section heading. Names may be wrapped in backticks.
candidates=$(awk '
  /^## .*([Dd]ecommissioned|[Aa]rchived)/ { in_section=1; next }
  /^## / { in_section=0 }
  in_section && /^\| *`?o-[a-z0-9-]+`? *\|/ {
    # Strip leading "| " then everything from the next "|" onward.
    sub(/^\| */, "", $0)
    sub(/ *\|.*$/, "", $0)
    # Strip surrounding backticks if present.
    gsub(/`/, "", $0)
    print
  }
' "$REGISTRY")

archived=0
skipped=0
would_archive=0

for orch in $candidates; do
  # Skip the caller's own orch (safety guard).
  if [ -n "$exclude_orch" ] && [ "$orch" = "$exclude_orch" ]; then
    skipped=$((skipped+1))
    continue
  fi

  src="$COMMS_DIR/$orch"
  [ -d "$src" ] || { skipped=$((skipped+1)); continue; }

  # Skip orchs with an active session (any timer file naming this orch).
  alive=false
  if [ -d "$TIMER_DIR" ]; then
    for af in "$TIMER_DIR"/*.agent; do
      [ -f "$af" ] || continue
      if [ "$(cat "$af" 2>/dev/null)" = "$orch" ]; then
        alive=true
        break
      fi
    done
  fi
  if [ "$alive" = true ]; then
    skipped=$((skipped+1))
    continue
  fi

  # Don't clobber an existing archived copy.
  if [ -e "$ARCHIVE_DIR/$orch" ]; then
    skipped=$((skipped+1))
    continue
  fi

  if [ "$dry_run" = true ]; then
    echo "[DRY-RUN] Would archive: comms/$orch -> comms/_archive/$orch"
    if [ -f "$AGENTS_DIR/$orch.md" ] && [ ! -e "$AGENT_ARCHIVE/$orch.md" ]; then
      echo "[DRY-RUN] Would archive: agents/$orch.md -> agents/_archive/$orch.md"
    fi
    would_archive=$((would_archive+1))
  else
    if mv "$src" "$ARCHIVE_DIR/$orch" 2>/dev/null; then
      echo "Archived: comms/$orch"
      if [ -f "$AGENTS_DIR/$orch.md" ] && [ ! -e "$AGENT_ARCHIVE/$orch.md" ]; then
        mv "$AGENTS_DIR/$orch.md" "$AGENT_ARCHIVE/$orch.md" 2>/dev/null && \
          echo "Archived: agents/$orch.md"
      fi
      archived=$((archived+1))
    else
      echo "WARN: failed to archive comms/$orch" >&2
      skipped=$((skipped+1))
    fi
  fi
done

if [ "$dry_run" = true ]; then
  echo "Would-archive: $would_archive | Skipped: $skipped"
else
  echo "Archived: $archived | Skipped: $skipped"
fi
