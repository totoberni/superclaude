#!/usr/bin/env bash
# cutover-md-cleanup.sh — superclaude v3 MD->DB cutover: recovery-tar (+ gated legacy delete).
#
# Memory and comms history now live in SQLite (.memory.db / .comms.db, with .broker.db as the
# live comms bus). This packs the legacy flat-MD corpus into a timestamped, verified recovery
# tarball BEFORE any deletion — turning the irreversible T8.5 delete into
# "recover-from-tarball-iff-something-is-missing".
#
# Usage:
#   cutover-md-cleanup.sh                 # tar-only (default): create + verify recovery archive
#   cutover-md-cleanup.sh --list-delete   # dry-run: list exactly what --delete WOULD remove
#   cutover-md-cleanup.sh --delete        # GATED: requires a fresh verified tarball; removes legacy MD
#
# The delete EXCLUDES operational scaffolding that is NOT replaced by the DB (see PRESERVE below).
set -uo pipefail

AM="$HOME/.claude/agent-memory"
COMMS="$HOME/.claude/comms"
BACKUP_DIR="$HOME/.claude/_backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$BACKUP_DIR/superclaude-md-legacy-$STAMP.tar.gz"

# --- PRESERVE: kept as operational/non-DB scaffolding, never deleted ---
#   the live DBs:            agent-memory/.memory.db, comms/.comms.db, comms/.broker.db
#   state recovery:          any path containing /_compact-snapshots/
#   lt-mem operational state: agent-memory/_system/lt-mem-checkpoint.md
#   KEEP-AS-FILE pointer:    agent-memory/w-doc/project_superclaude_v3_cutover.md
#   (comms bootstrap/state/plan/context/meta-registry and plans/** are out of scope by construction)
is_preserved() {
  case "$1" in
    */_compact-snapshots/*) return 0 ;;
    "$AM/_system/lt-mem-checkpoint.md") return 0 ;;
    "$AM/w-doc/project_superclaude_v3_cutover.md") return 0 ;;
    *) return 1 ;;
  esac
}

# Legacy MD in cutover scope: every *.md under agent-memory + mined flat comms snapshots.
list_all_legacy_md() {
  find "$AM" -type f -name '*.md'
  find "$COMMS" -type f \( -name 'directives.md' -o -name 'reports.md' -o -name 'escalations.md' \)
}

# What --delete would remove = legacy MD minus the PRESERVE set.
list_delete_targets() {
  local f
  while IFS= read -r f; do
    is_preserved "$f" || printf '%s\n' "$f"
  done < <(list_all_legacy_md)
}

create_tar() {
  mkdir -p "$BACKUP_DIR"
  echo "Packing FULL legacy MD corpus (incl. preserved, for max recoverability) -> $ARCHIVE"
  # tar everything in scope so the backup is a superset of anything we might delete.
  list_all_legacy_md | tar czf "$ARCHIVE" -T - 2>/dev/null
  local n_src n_tar
  n_src=$(list_all_legacy_md | wc -l)
  n_tar=$(tar tzf "$ARCHIVE" 2>/dev/null | wc -l)
  echo "  source MD files : $n_src"
  echo "  archived entries: $n_tar"
  if [ "$n_src" -eq "$n_tar" ] && [ "$n_tar" -gt 0 ]; then
    echo "  OK verified -> $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
    return 0
  fi
  echo "  ERROR: count mismatch or empty archive — NOT safe to delete"
  return 1
}

case "${1:---tar-only}" in
  --tar-only)
    create_tar
    ;;
  --list-delete)
    echo "=== --delete would remove these (legacy MD minus PRESERVE) ==="
    list_delete_targets
    echo "--- preserved (kept) ---"
    list_all_legacy_md | while IFS= read -r f; do is_preserved "$f" && printf '  KEEP %s\n' "$f"; done
    ;;
  --delete)
    echo "GATED DELETE — re-creating a fresh verified tarball first..."
    create_tar || { echo "ABORT: backup failed, nothing deleted."; exit 1; }
    echo "Deleting legacy MD (PRESERVE set excluded)..."
    n=0
    while IFS= read -r f; do rm -f "$f" && n=$((n+1)); done < <(list_delete_targets)
    echo "Deleted $n legacy MD files. Recovery archive: $ARCHIVE"
    ;;
  *)
    echo "usage: cutover-md-cleanup.sh [--tar-only|--list-delete|--delete]"; exit 2 ;;
esac
