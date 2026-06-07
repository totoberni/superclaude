#!/usr/bin/env bash
# hcom-init.sh — initialize HCOM SQLite database
# Idempotent: safe to run multiple times.
# Usage:
#   hcom-init.sh           # create DB if missing, apply schema
#   hcom-init.sh --reset   # DROP + recreate (destructive — confirms)
#   hcom-init.sh --status  # show current state

set -e

DB_DIR="$HOME/.claude/comms"
DB_PATH="$DB_DIR/.broker.db"
mkdir -p "$DB_DIR"

# Add to .gitignore protection (DB shouldn't be committed)
gitignore="$DB_DIR/.gitignore"
if [ ! -f "$gitignore" ] || ! grep -q ".broker.db" "$gitignore" 2>/dev/null; then
  echo ".broker.db" >> "$gitignore"
  echo ".broker.db-journal" >> "$gitignore"
  echo ".broker.db-wal" >> "$gitignore"
  echo ".broker.db-shm" >> "$gitignore"
fi

case "${1:-}" in
  --reset)
    read -p "DESTRUCTIVE: drop $DB_PATH and recreate? [y/N] " ans
    [ "$ans" = "y" ] || { echo "Cancelled."; exit 0; }
    rm -f "$DB_PATH" "$DB_PATH-journal" "$DB_PATH-wal" "$DB_PATH-shm"
    echo "Dropped. Recreating..."
    ;;
  --status)
    if [ -f "$DB_PATH" ]; then
      echo "DB: $DB_PATH"
      ls -la "$DB_PATH"
      sqlite3 "$DB_PATH" 'SELECT COUNT(*) AS messages FROM messages;' 2>/dev/null
      sqlite3 "$DB_PATH" 'SELECT COUNT(*) AS agents FROM agent_status;' 2>/dev/null
      sqlite3 "$DB_PATH" 'SELECT COUNT(*) AS locks FROM file_locks;' 2>/dev/null
    else
      echo "DB not initialized: $DB_PATH"
    fi
    exit 0
    ;;
esac

# Check for sqlite3
command -v sqlite3 >/dev/null 2>&1 || { echo "Error: sqlite3 not installed. Install via package manager."; exit 1; }

# Apply schema (heredoc embedded; idempotent via IF NOT EXISTS)
sqlite3 "$DB_PATH" <<'SCHEMA_END'
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  from_agent TEXT NOT NULL,
  to_agent TEXT NOT NULL,
  kind TEXT NOT NULL,
  seq INTEGER,
  body TEXT NOT NULL,
  read_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_to_unread ON messages(to_agent, read_at) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_kind_seq ON messages(kind, seq);

CREATE TABLE IF NOT EXISTS agent_status (
  agent TEXT PRIMARY KEY,
  pid INTEGER,
  started_at INTEGER NOT NULL,
  last_active_at INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'IDLE'
);

CREATE TABLE IF NOT EXISTS file_locks (
  path TEXT PRIMARY KEY,
  locked_by TEXT NOT NULL,
  acquired_at INTEGER NOT NULL,
  ttl_sec INTEGER NOT NULL DEFAULT 30
);
SCHEMA_END

echo "HCOM DB initialized: $DB_PATH"
sqlite3 "$DB_PATH" '.schema' 2>/dev/null | head -30
