#!/usr/bin/env bash
# Hook: stop
# Event: Stop (graceful stop, before SessionEnd)
# Purpose: snapshot any unsaved worker output / state before context discard.
# Mirrors pre-compact.sh pattern but for the Stop event — captures
# autocommissioned ephemeral agents and recent spawn telemetry so a
# discarded session leaves a recoverable trace.

set -uo pipefail

input=$(cat)
session_id=$(echo "$input" | jq -r '.session_id // ""' 2>/dev/null) || session_id=""
[ -z "$session_id" ] && exit 0

snapshot_root="$HOME/.claude/agent-memory/_system/_stop-snapshots"
timestamp=$(date +%Y%m%d-%H%M%S)
snapshot_dir="${snapshot_root}/${session_id}-${timestamp}"
mkdir -p "$snapshot_dir"

# Snapshot _ephemeral/ agents (autocommissioned workers may have produced output)
if [ -d "$HOME/.claude/agents/_ephemeral" ]; then
  cp -r "$HOME/.claude/agents/_ephemeral" "$snapshot_dir/" 2>/dev/null || true
fi

# Snapshot recent spawn log (last 100 lines)
spawn_log="$HOME/.claude/comms/_spawns.log"
[ -f "$spawn_log" ] && tail -100 "$spawn_log" > "$snapshot_dir/recent-spawns.log" 2>/dev/null || true

# Keep only last 5 stop snapshots per session-id (rolling, prevents unbounded growth)
all_snapshots=$(ls -dt "${snapshot_root}/${session_id}-"* 2>/dev/null | tail -n +6)
[ -n "$all_snapshots" ] && rm -rf $all_snapshots

exit 0
