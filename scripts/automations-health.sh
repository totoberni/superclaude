#!/bin/bash
# automations-health.sh — standalone READ-ONLY health probe for the toto
# automation runtime (ntfy, discovery/WireGuard egress, Remote-Control coding
# plane, W3 automation engine, W4 JobHunt pipeline). Runs on WSL, probes toto
# over the tsh channel. Also folds in one WSL-side check (the W5 engine dev
# test-suite, which is built + tested on this box but not yet deployed on toto)
# and carries a gated, deferred block of W5-runtime checks that stay dormant
# (skip) until the W5 stack deploys on toto (see §W5 in the collector).
#
# SINGLE-SOURCE: toto-side check logic lives ONCE, in the collector heredoc that
# is shipped to and executed on toto; the WSL side never reimplements a toto
# check. WSL-only checks (the engine dev test-suite) run locally on this box.
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
# right now", so it does NOT penalize the toto checks. The unreachable path
# still runs and scores the WSL-side checks (engine dev test-suite) honestly;
# when those are skipped/absent too, it prints "SCORE: 100/100".
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
engine, W4 JobHunt pipeline). Runs on WSL; probes toto over the tsh ssh
channel.

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

# ── Check registry (single source of truth for labels + report order) ───────
# Toto-side keys are emitted by the collector shipped to toto and run THERE
# (single-source: the check logic lives once, in the heredoc below, and is not
# reimplemented on the WSL side). WSL-side keys (WSL_KEYS) are computed locally
# on this dev box. w5_* keys are DEFERRED W5-runtime checks: they emit `skip`
# (excluded from the score) until the W5 stack is deployed on toto (see the
# §W5 block in the collector), so they never penalize today's score.
declare -A LABELS=(
  [ntfy_health]="ntfy /v1/health"
  [ntfy_service]="ntfy.service (system)"
  [ntfy_listener]="ntfy-listener.service (user)"
  [discovery_gluetun]="discovery-gluetun healthy"
  [discovery_browser]="discovery-browser running"
  [rc_sessions]="RC tmux sessions alive"
  [rc_login_health]="RC login-health proxy"
  [engine_build_dir]="engine-build deployed"
  [jobhunt_dir]="jobhunt runtime dir + store.db"
  [jobhunt_timer]="jobhunt-daily.timer state"
  [jobhunt_freshness]="jobhunt runs.jsonl freshness"
  [engine_testsuite]="engine dev test-suite (WSL)"
  [w5_bw_serve]="W5 vault bw serve daemon"
  [w5_inbox_imap]="W5 dedicated-inbox IMAP reachable"
  [w5_patchright_chrome]="W5 patchright chrome installed"
  [w5_provider_contract]="W5 provider-contract test"
)

# WSL-side check keys: computed on THIS box, independent of toto reachability.
WSL_KEYS=(engine_testsuite)

# Toto-side check keys, in report order. The w5_* keys trail the stable set and
# stay gated (skip) until the W5 deploy marker appears (see §W5).
CHECK_ORDER=(ntfy_health ntfy_service ntfy_listener discovery_gluetun discovery_browser rc_sessions rc_login_health engine_build_dir jobhunt_dir jobhunt_timer jobhunt_freshness w5_bw_serve w5_inbox_imap w5_patchright_chrome w5_provider_contract)

WSL_RESULTS=""          # "key=pass|fail|skip" lines from WSL-side checks
declare -A EXTRA=()     # optional per-key detail suffix, appended by the scorer

# score_checks <merged key=val output> <key…>
# Shared scorer for BOTH the normal path (WSL + toto keys) and the toto-
# unreachable path (WSL keys only). Denominator-honest: pass / pass_gated count
# toward PASS+TOTAL; skip counts toward neither; any other value (including a
# key absent from the output) is a FAIL toward TOTAL. TOTAL==0 (nothing
# measurable) yields 100/100, matching the "nothing to penalize" convention.
# Prints one line per check and, as the final line, exactly "SCORE: <int>/100".
score_checks() {
  local merged="$1"; shift
  local total=0 pass=0 key val extra
  for key in "$@"; do
    val=$(printf '%s\n' "$merged" | grep -oE "^${key}=[a-z_]+$" | tail -1 | cut -d= -f2)
    extra="${EXTRA[$key]:-}"; [ -n "$extra" ] && extra=" ($extra)"
    case "$val" in
      pass)
        total=$((total + 1)); pass=$((pass + 1))
        echo "automations-health: ${LABELS[$key]}: ok${extra}" ;;
      pass_gated)
        total=$((total + 1)); pass=$((pass + 1))
        echo "automations-health: ${LABELS[$key]}: ok (gated, pre-enable)${extra}" ;;
      skip)
        echo "automations-health: ${LABELS[$key]}: SKIP (deferred/gated, excluded from score)${extra}" ;;
      *)
        total=$((total + 1))
        echo "automations-health: ${LABELS[$key]}: FAIL${extra}" ;;
    esac
  done
  local score=100
  [ "$total" -gt 0 ] && score=$((pass * 100 / total))
  echo "SCORE: $score/100"
}

na_exit() {
  echo "automations-health: toto N/A (unreachable, no penalty on toto checks)"
  # WSL-side checks do not need toto; score them honestly even when toto is dark.
  score_checks "$WSL_RESULTS" "${WSL_KEYS[@]}"
  exit 0
}

# ── WSL-side check: engine dev test-suite green ─────────────────────────────
# The W5 engine is BUILT + unit-tested on this WSL dev box but NOT YET deployed
# on toto (a later live-acceptance phase). This runs the FULL dev suite locally
# (every package, incl. tests/test_providers_*, test_ingest_*, test_validate_*,
# via pytest.ini `testpaths = tests`) and folds the verdict into the score,
# independent of toto reachability. Detect-if-present: an absent engine-build
# gracefully skips (no penalty). `-p no:cacheprovider` avoids a .pytest_cache
# write when invoked from a write-restricted (sandboxed) context.
run_wsl_engine_testsuite() {
  local root="$HOME/automations/engine-build"
  local py="$root/.venv-dev/bin/python"
  if [ ! -x "$py" ] || [ ! -f "$root/pytest.ini" ]; then
    WSL_RESULTS="${WSL_RESULTS}engine_testsuite=skip"$'\n'
    EXTRA[engine_testsuite]="engine-build absent on this box"
    return 0
  fi
  local out rc clean passed fails
  out="$(cd "$root" && "$py" -m pytest -q -p no:cacheprovider 2>&1)"; rc=$?
  clean="$(printf '%s' "$out" | sed -r 's/\x1b\[[0-9;]*m//g')"
  passed="$(printf '%s\n' "$clean" | grep -oE '[0-9]+ passed' | tail -1)"
  fails="$(printf '%s\n' "$clean" | grep -oE '[0-9]+ (failed|errors?)' | tail -1)"
  if [ "$rc" -eq 0 ] && [ -n "$passed" ] && [ -z "$fails" ]; then
    WSL_RESULTS="${WSL_RESULTS}engine_testsuite=pass"$'\n'
    EXTRA[engine_testsuite]="${passed}, exit 0"
  else
    WSL_RESULTS="${WSL_RESULTS}engine_testsuite=fail"$'\n'
    EXTRA[engine_testsuite]="exit ${rc}; ${passed:-0 passed}${fails:+; $fails}"
  fi
}
run_wsl_engine_testsuite

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

ok()    { echo "$1=pass"; }
bad()   { echo "$1=fail"; }
gated() { echo "$1=pass_gated"; }  # ok, but deliberately gated (pre-enable)
skip()  { echo "$1=skip"; }        # graceful-skip: excluded from denominator

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

# 9. W4 JobHunt runtime dir present with store.db
if [ -d "$HOME/automations/jobhunt" ] && [ -f "$HOME/automations/jobhunt/store.db" ]; then
  ok jobhunt_dir
else
  bad jobhunt_dir
fi

# 10+11 share one read of jobhunt-daily.timer state (set -u safe defaults).
timer_present=""
timer_enabled=""
timer_active=""
if systemctl --user list-unit-files jobhunt-daily.timer --no-legend 2>/dev/null \
    | grep -q jobhunt-daily.timer; then
  timer_present="yes"
  timer_enabled="$(systemctl --user is-enabled jobhunt-daily.timer 2>/dev/null || true)"
  timer_active="$(systemctl --user is-active jobhunt-daily.timer 2>/dev/null || true)"
fi

# 10. jobhunt-daily.timer state. The timer is deliberately installed-but-
# disabled pending an owner cost/grounding gate; that state is NOT a failure.
# Only missing unit files (never installed) are a failure.
if [ -z "$timer_present" ]; then
  bad jobhunt_timer
elif [ "$timer_enabled" = "enabled" ] && [ "$timer_active" = "active" ]; then
  ok jobhunt_timer
elif [ "$timer_enabled" = "disabled" ]; then
  gated jobhunt_timer
else
  # enabled-but-not-active (or any other unexpected state) is a real problem.
  bad jobhunt_timer
fi

# 11. runs.jsonl freshness. Only meaningful once the timer gate is open;
# while gated-disabled, graceful-skip (excluded from the scoring denominator,
# same convention as the top-level toto-unreachable N/A path above).
if [ "$timer_enabled" != "enabled" ]; then
  skip jobhunt_freshness
else
  runs_file="$HOME/automations/jobhunt/runs.jsonl"
  last_ts=""
  if [ -f "$runs_file" ]; then
    last_line="$(grep -v '^[[:space:]]*$' "$runs_file" 2>/dev/null | tail -n 1)"
    # Defensive ts extraction: last line's "ts" JSON string field, no jq dep.
    last_ts="$(printf '%s' "$last_line" \
      | grep -oE '"ts"[[:space:]]*:[[:space:]]*"[^"]+"' \
      | sed -E 's/.*"([^"]+)"$/\1/')"
  fi
  last_epoch=""
  if [ -n "$last_ts" ]; then
    last_epoch="$(date -d "$last_ts" +%s 2>/dev/null || true)"
    if [ -z "$last_epoch" ]; then
      # Fall back to a raw epoch integer if the ts field wasn't ISO8601.
      case "$last_ts" in
        ''|*[!0-9]*) last_epoch="" ;;
        *) last_epoch="$last_ts" ;;
      esac
    fi
  fi
  if [ -n "$last_epoch" ] && [ $(( $(date +%s) - last_epoch )) -le $(( 26 * 3600 )) ]; then
    ok jobhunt_freshness
  else
    bad jobhunt_freshness
  fi
fi

# ── §W5: DEFERRED W5-runtime checks (gated; skip until deploy) ───────────────
# The W5 stack (engine/providers Patchright browser layer, engine/ingest
# dedicated inbox, engine/validate anti-injection, the Vaultwarden vault +
# `bw serve` daemon) is BUILT + unit-tested on the WSL dev box but NOT YET
# DEPLOYED here on toto. Live probes against undeployed services would FAIL, so
# this whole block is GATED behind a single deploy signal and emits `skip` (no
# score penalty) until then.
#
# ACTIVATION: the W5 live-acceptance deploy phase creates the marker
#   ~/automations/engine-build/.w5-deployed        (or export W5_DEPLOYED=1)
# after which the four real probes below run. Deploy-time checklist:
#   * w5_bw_serve          : Vaultwarden `bw serve` daemon up (127.0.0.1:8087)
#   * w5_inbox_imap        : dedicated-inbox IMAP host:port reachable
#   * w5_patchright_chrome : patchright + chrome present in the runtime venv
#   * w5_provider_contract : parametrized provider-contract pytest passes
# Constants tagged `deploy-confirm:` must be reconciled with the actual W5
# deploy layout before the marker is flipped on.
W5_MARKER="$HOME/automations/engine-build/.w5-deployed"
if [ -n "${W5_DEPLOYED:-}" ] || [ -f "$W5_MARKER" ]; then
  # Runtime host/port/paths are read from a gitignored env file the deploy
  # phase writes; never hardcode machine-specific values here.
  W5_ENV="$HOME/automations/engine-build/.w5-deployed.env"
  [ -f "$W5_ENV" ] && . "$W5_ENV" 2>/dev/null || true
  RT_PY="$HOME/automations/engine-build/.venv/bin/python"   # deploy-confirm: runtime venv path
  RT_ROOT="$HOME/automations/engine-build"

  # bw serve daemon reachable. deploy-confirm: port/endpoint of `bw serve`.
  if curl -s --max-time 4 "${W5_BW_SERVE_URL:-http://127.0.0.1:8087/status}" 2>/dev/null | grep -q '"'; then
    ok w5_bw_serve
  else
    bad w5_bw_serve
  fi

  # dedicated-inbox IMAP reachable (plain TCP connect, no auth, no secrets).
  # deploy-confirm: W5_IMAP_HOST/PORT come from the W5 env file above.
  if [ -n "${W5_IMAP_HOST:-}" ] \
      && timeout 5 bash -c "exec 3<>/dev/tcp/${W5_IMAP_HOST}/${W5_IMAP_PORT:-993}" 2>/dev/null; then
    ok w5_inbox_imap
  else
    bad w5_inbox_imap
  fi

  # patchright + chrome installed in the runtime venv.
  if [ -x "$RT_PY" ] && "$RT_PY" -c 'import patchright' 2>/dev/null; then
    ok w5_patchright_chrome
  else
    bad w5_patchright_chrome
  fi

  # provider-contract pytest against the deployed engine.
  # deploy-confirm: the contract test module/marker for the provider layer.
  if [ -x "$RT_PY" ] && ( cd "$RT_ROOT" \
        && "$RT_PY" -m pytest -q -p no:cacheprovider tests/test_providers_registry.py >/dev/null 2>&1 ); then
    ok w5_provider_contract
  else
    bad w5_provider_contract
  fi
else
  skip w5_bw_serve
  skip w5_inbox_imap
  skip w5_patchright_chrome
  skip w5_provider_contract
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

# ── Parse + score ───────────────────────────────────────────────────────────
# Merge the WSL-side results (computed locally, above) with the toto collector
# output and score both together via the shared scorer (single definition, used
# here and in na_exit). Denominator-honest: skip'd checks (jobhunt_freshness
# while its timer gate is closed; all w5_* while pre-deploy) count toward
# neither PASS nor TOTAL. score_checks emits the final "SCORE: <int>/100" line.
MERGED="$(printf '%s\n%s\n' "$WSL_RESULTS" "$COLLECT_OUT")"
score_checks "$MERGED" "${WSL_KEYS[@]}" "${CHECK_ORDER[@]}"
exit 0
