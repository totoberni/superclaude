#!/bin/bash
# automations-health.sh — standalone READ-ONLY health probe for the toto
# automation runtime (ntfy, discovery/WireGuard egress, Remote-Control coding
# plane, W3 automation engine). Runs on WSL, probes toto over the tsh channel.
#
# This probe is deliberately NOT wired to page anyone. It is the basis for
# the future W7 watchdog (proactively pages abe-alerts when toto 401s or
# goes dark) — paging itself is out of scope here; this script only
# measures and reports.
#
# Usage:
#   bash automations-health.sh [--help]
#
# Env:
#   TSH_TIMEOUT   seconds before giving up on toto (default 25)
#
# Contract: prints per-check pass/fail detail lines and, as its FINAL stdout
# line, exactly "SCORE: <int>/100". Exit code is ALWAYS 0 (never fails the
# caller, even when toto is unreachable — see GRACEFUL-SKIP below).
#
# GRACEFUL-SKIP: if the ssh channel to toto is unreachable or times out, this
# script cannot tell "toto is down" apart from "this WSL box has no network
# right now", so it does NOT penalize the score. It prints
# "automations-health: N/A (toto unreachable, no penalty)" and
# "SCORE: 100/100".
#
# SECRETS: this script and its remote collector NEVER print ntfy
# credentials, the WireGuard private key, or any other secret. Only
# pass/fail per check.
#
# Implementation note (mandatory pattern — see rules/20-tool-conventions.md
# § toto Remote Ops and the fish/$/brace gotcha): toto's login shell is
# fish, and tsh passes ONE command string per call, so a compound remote
# command containing `$` or `{...}` risks fish-parsing breakage. Instead of
# inlining a compound remote command through tsh, this script ships a small
# bash COLLECTOR script to toto via scp, then runs it with
# `tsh 'bash /tmp/automations-collect.sh'` — bash on toto is safe for all
# `$`/`{}` constructs.

set -uo pipefail   # never set -e — every check must run to completion

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  cat <<'HELP'
Usage: automations-health.sh [--help]

Standalone READ-ONLY health probe for the toto automation runtime (ntfy,
discovery/WireGuard egress, Remote-Control coding plane, W3 automation
engine). Runs on WSL; probes toto over the tsh ssh channel.

Prints per-check pass/fail detail lines and a final "SCORE: <int>/100"
line. Exit code is always 0, even when toto is unreachable (graceful-skip:
prints "automations-health: N/A (toto unreachable, no penalty)" and
"SCORE: 100/100").

This probe is the basis for the future W7 watchdog that will page
abe-alerts when toto 401s or goes dark (paging is W7, not implemented
here).

Env:
  TSH_TIMEOUT   seconds before giving up on toto (default 25)
HELP
  exit 0
fi

CLAUDE="${CLAUDE_DIR:-$HOME/.claude}"
TSH="$CLAUDE/bin/tsh"
SOCK="$HOME/.ssh/agent.sock"
REMOTE_COLLECTOR="/tmp/automations-collect.sh"

export TSH_TIMEOUT="${TSH_TIMEOUT:-25}"

na_exit() {
  echo "automations-health: N/A (toto unreachable, no penalty)"
  echo "SCORE: 100/100"
  exit 0
}

# Machine-specific toto connection params (Tailscale IP + ntfy port) live in a
# gitignored config, never hardcoded here (public repo). Absent config => cannot
# probe => graceful N/A. See config/toto.env.example.
TOTO_ENV="$CLAUDE/config/toto.env"
[ -f "$TOTO_ENV" ] && . "$TOTO_ENV"
TOTO_IP="${TOTO_TAILSCALE_IP:-}"
NTFY_PORT="${NTFY_PORT:-2586}"
[ -n "$TOTO_IP" ] || na_exit
NTFY_HEALTH_URL="http://${TOTO_IP}:${NTFY_PORT}/v1/health"

LOCAL_COLLECTOR=$(mktemp "${TMPDIR:-/tmp}/automations-collect.XXXXXX.sh") || na_exit
cleanup() {
  rm -f "$LOCAL_COLLECTOR"
  # Best-effort remote cleanup; never let this hang or fail the script.
  timeout 10 "$TSH" "rm -f $REMOTE_COLLECTOR" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ── Collector script (bash, runs ON toto) ──────────────────────────────────
# The Tailscale IP:port comes from config (never hardcoded); inject it as a
# variable line ahead of the quoted heredoc body (%q-quoted so it is safe).
{
  printf '#!/bin/bash\n'
  printf 'NTFY_HEALTH_URL=%q\n' "$NTFY_HEALTH_URL"
  cat <<'COLLECTOR'
# automations-collect.sh — runs ON toto (bash, not fish). Read-only. Never
# prints secrets. One "<key>=pass|fail" line per check.
set -uo pipefail

ok()  { echo "$1=pass"; }
bad() { echo "$1=fail"; }

# 1. ntfy /v1/health
if curl -s --max-time 5 "$NTFY_HEALTH_URL" 2>/dev/null | grep -q '"healthy":true'; then
  ok ntfy_health
else
  bad ntfy_health
fi

# 2. ntfy.service (system unit)
if [ "$(systemctl is-active ntfy.service 2>/dev/null)" = "active" ]; then
  ok ntfy_service
else
  bad ntfy_service
fi

# 3. ntfy-listener.service (user unit)
if [ "$(systemctl --user is-active ntfy-listener.service 2>/dev/null)" = "active" ]; then
  ok ntfy_listener
else
  bad ntfy_listener
fi

# 4. discovery-gluetun container healthy
if [ -n "$(docker ps --filter name=discovery-gluetun --filter health=healthy -q 2>/dev/null)" ]; then
  ok discovery_gluetun
else
  bad discovery_gluetun
fi

# 5. discovery-browser container running
if [ -n "$(docker ps --filter name=discovery-browser --filter status=running -q 2>/dev/null)" ]; then
  ok discovery_browser
else
  bad discovery_browser
fi

# 6. Remote-Control tmux sessions alive
if tmux has-session -t rc-automations 2>/dev/null && tmux has-session -t rc-superclaude 2>/dev/null; then
  ok rc_sessions
else
  bad rc_sessions
fi

# 7. RC login-health proxy: neither RC pane shows a broken OAuth/API login.
rc_panes="$(tmux capture-pane -pt rc-automations 2>/dev/null)"
rc_panes="$rc_panes
$(tmux capture-pane -pt rc-superclaude 2>/dev/null)"
if printf '%s' "$rc_panes" | grep -qiE 'failed to connect|401'; then
  bad rc_login_health
else
  ok rc_login_health
fi

# 8. W3 automation engine deployed
if [ -d "$HOME/automations/engine-build" ]; then
  ok engine_build_dir
else
  bad engine_build_dir
fi
COLLECTOR
} > "$LOCAL_COLLECTOR"
chmod +x "$LOCAL_COLLECTOR"

# ── Ship + run on toto ──────────────────────────────────────────────────────
if ! [ -S "$SOCK" ] || ! timeout 10 env SSH_AUTH_SOCK="$SOCK" ssh-add -l >/dev/null 2>&1; then
  na_exit
fi

if ! timeout "$TSH_TIMEOUT" env SSH_AUTH_SOCK="$SOCK" scp -q -o BatchMode=yes -o ConnectTimeout=10 \
    "$LOCAL_COLLECTOR" "toto:$REMOTE_COLLECTOR" >/dev/null 2>&1; then
  na_exit
fi

COLLECT_OUT=$("$TSH" "bash $REMOTE_COLLECTOR" 2>/dev/null)
TSH_RC=$?
if [ "$TSH_RC" -ne 0 ] || [ -z "$COLLECT_OUT" ]; then
  na_exit
fi

# ── Parse + score (8 checks, each an equal slice of 100) ──────────────────
declare -A LABELS=(
  [ntfy_health]="ntfy /v1/health"
  [ntfy_service]="ntfy.service (system)"
  [ntfy_listener]="ntfy-listener.service (user)"
  [discovery_gluetun]="discovery-gluetun healthy"
  [discovery_browser]="discovery-browser running"
  [rc_sessions]="RC tmux sessions alive"
  [rc_login_health]="RC login-health proxy"
  [engine_build_dir]="engine-build deployed"
)
CHECK_ORDER=(ntfy_health ntfy_service ntfy_listener discovery_gluetun discovery_browser rc_sessions rc_login_health engine_build_dir)

TOTAL=${#CHECK_ORDER[@]}
PASS=0
for key in "${CHECK_ORDER[@]}"; do
  val=$(printf '%s\n' "$COLLECT_OUT" | grep -oE "^${key}=(pass|fail)$" | tail -1 | cut -d= -f2)
  if [ "$val" = "pass" ]; then
    PASS=$((PASS + 1))
    echo "automations-health: ${LABELS[$key]}: ok"
  else
    echo "automations-health: ${LABELS[$key]}: FAIL"
  fi
done

SCORE=$((PASS * 100 / TOTAL))
echo "SCORE: $SCORE/100"
exit 0
