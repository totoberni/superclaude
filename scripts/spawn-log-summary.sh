#!/usr/bin/env bash
# spawn-log-summary.sh — analyze ~/.claude/comms/_spawns.log
# Usage: spawn-log-summary.sh [--by-type|--by-parent|--recent] [<since>]
#   <since> is an ISO-8601 prefix (e.g. 2026-05-09 or 2026-05-09T16) used as
#   a string lower-bound filter on the timestamp column.
# Referenced from: ~/.claude/plans/swarm-first-v2/validation-rubric.md

set -e
LOG="$HOME/.claude/comms/_spawns.log"
[ -f "$LOG" ] || { echo "No spawn log found"; exit 0; }

mode="${1:-summary}"
since="${2:-}"

filter() {
  if [ -n "$since" ]; then
    awk -F'\t' -v since="$since" '$1 >= since' "$LOG"
  else
    cat "$LOG"
  fi
}

case "$mode" in
  --by-type)
    filter | awk -F'\t' '{print $3}' | sort | uniq -c | sort -rn
    ;;
  --by-parent)
    filter | awk -F'\t' '{print $2}' | sort | uniq -c | sort -rn
    ;;
  --recent)
    filter | tail -20
    ;;
  *)
    echo "## Spawn Log Summary"
    echo "Total events: $(filter | wc -l)"
    echo ""
    echo "By subagent_type:"
    filter | awk -F'\t' '{print $3}' | sort | uniq -c | sort -rn | head -10
    echo ""
    echo "By parent agent:"
    filter | awk -F'\t' '{print $2}' | sort | uniq -c | sort -rn | head -10
    echo ""
    echo "Last 5 events:"
    filter | tail -5
    ;;
esac
