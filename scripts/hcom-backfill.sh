#!/usr/bin/env bash
# hcom-backfill.sh — backfill historical comms into HCOM SQLite broker.
# Idempotent (audit table prevents duplicates). Default = dry-run.
#
# Usage:
#   hcom-backfill.sh                          # dry-run, active orchs only
#   hcom-backfill.sh --apply                  # actually insert
#   hcom-backfill.sh --apply --archive        # include _archive/
#   hcom-backfill.sh --orch o-example          # one orch
#   hcom-backfill.sh --apply --orch o-example  # apply for one orch

set -e

DB_PATH="$HOME/.claude/comms/.broker.db"
COMMS_DIR="$HOME/.claude/comms"

[ -f "$DB_PATH" ] || { echo "HCOM DB not initialized. Run hcom-init.sh first."; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "sqlite3 CLI required."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 required."; exit 1; }
PYTHON="$HOME/.claude/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="$(command -v python3 2>/dev/null || true)"

# Defaults
mode="dry-run"
include_archive=false
target_orch=""

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) mode="apply"; shift ;;
    --archive) include_archive=true; shift ;;
    --orch) target_orch="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# Ensure audit table
sqlite3 "$DB_PATH" "CREATE TABLE IF NOT EXISTS backfill_audit (ikey TEXT PRIMARY KEY, inserted_at INTEGER NOT NULL);"

# Collect orch dirs
dirs=()
if [ -n "$target_orch" ]; then
  [ -d "$COMMS_DIR/$target_orch" ] && dirs+=("$COMMS_DIR/$target_orch")
  [ "$include_archive" = true ] && [ -d "$COMMS_DIR/_archive/$target_orch" ] && dirs+=("$COMMS_DIR/_archive/$target_orch")
else
  for d in "$COMMS_DIR"/*/; do
    name=$(basename "$d")
    case "$name" in _archive|_template) continue ;; esac
    dirs+=("$d")
  done
  if [ "$include_archive" = true ] && [ -d "$COMMS_DIR/_archive" ]; then
    for d in "$COMMS_DIR"/_archive/*/; do
      [ -d "$d" ] && dirs+=("$d")
    done
  fi
fi

# Helper: process one comms file via hcom_backfill.py
process_file() {
  local file="$1" orch="$2" kind="$3" from="$4" to="$5"
  [ -f "$file" ] || return 0

  "$PYTHON" "$HOME/.claude/scripts/hcom_backfill.py" \
    "$file" "$orch" "$kind" "$from" "$to" "$DB_PATH" "$mode"
}

# Iterate dirs
for d in "${dirs[@]}"; do
  # Strip trailing slash; orch name = leaf
  d="${d%/}"
  orch=$(basename "$d")

  echo "=== $orch ==="

  # Skip the directives.md INDEX file when split directives/ exists (avoids double-count)
  has_split_dir=false
  [ -d "$d/directives" ] && has_split_dir=true

  # directives.md (only if not split)
  if [ -f "$d/directives.md" ] && [ "$has_split_dir" = false ]; then
    process_file "$d/directives.md" "$orch" "DIR" "meta" "@$orch"
  fi

  # split directives/DIR-NNN.md
  if [ "$has_split_dir" = true ]; then
    for df in "$d"/directives/DIR-*.md; do
      [ -f "$df" ] || continue
      process_file "$df" "$orch" "DIR" "meta" "@$orch"
    done
  fi

  # reports
  [ -f "$d/reports.md" ] && process_file "$d/reports.md" "$orch" "RPT" "$orch" "meta"

  # escalations
  [ -f "$d/escalations.md" ] && process_file "$d/escalations.md" "$orch" "ESC" "$orch" "meta"
done

echo ""
echo "=== Summary ==="
total_msgs=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM messages;")
total_audit=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM backfill_audit;")
echo "messages rows: $total_msgs | backfill_audit rows: $total_audit"
echo "Mode: $mode | include_archive: $include_archive | target_orch: ${target_orch:-<all>}"
