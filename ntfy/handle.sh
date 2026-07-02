#!/usr/bin/env bash
# ntfy free-text command handler (W1 skeleton).
# Receives one message via ntfy env vars, appends it to the durable inbox,
# and acks on abe-alerts so the round-trip is visible on the phone.
# W5 replaces the body with the real dispatcher (ID resolution + SDK session resume).
set -euo pipefail

INBOX_DIR="$HOME/automations/ntfy"
INBOX="$INBOX_DIR/inbox.jsonl"
CRED="$INBOX_DIR/credentials"
mkdir -p "$INBOX_DIR"

ts="$(date -Is)"
topic="${NTFY_TOPIC:-unknown}"
msg="${NTFY_MESSAGE:-}"

# Durable append (jq-free JSON escaping via python to survive arbitrary free text).
python3 - "$ts" "$topic" "$msg" >> "$INBOX" <<'PYEOF'
import json, sys
print(json.dumps({"ts": sys.argv[1], "topic": sys.argv[2], "message": sys.argv[3], "state": "received"}))
PYEOF

# Ack push on abe-alerts. Token AND base URL are read from the 0600 credentials
# file (never inlined; keeps the machine-specific IP out of this tracked script).
token="$(grep '^token=' "$CRED" | cut -d= -f2-)"
base_url="$(grep '^url=' "$CRED" | cut -d= -f2-)"
curl -s -m 10 \
  -H "Authorization: Bearer $token" \
  -H "Title: dispatcher ack" \
  -d "received on ${topic}: ${msg}" \
  "${base_url}/abe-alerts" > /dev/null || true
