# Module: SKM per-session ephemeral toto credential (SessionStart).
# No-op unless the ENABLED marker exists (lockout-safe rollout). Only meta/orch sessions touch
# toto, so only they mint. Backgrounded so a toto round-trip never delays session start; the
# wrapper's `skm ensure` and the toto prune timer are the backstops if this does not complete.
# Reads: AGENT_NAME, SESSION_ID, TIMER_DIR.

mod_skm_session() {
  [ -f "$HOME/.ssh/skm/ENABLED" ] || return 0
  [ -x "$HOME/.claude/bin/skm" ] || return 0
  [ "${SKM_DISABLE_MINT:-0}" = "1" ] && return 0     # test/health harnesses opt out
  case "$AGENT_NAME" in meta|orch|orch-*) ;; *) return 0 ;; esac
  # Skip synthetic session ids used by hook tests / perf probes (they must not cause a real
  # toto registration when a harness simulates a SessionStart under a live meta/orch process).
  case "$SESSION_ID" in ""|unknown|test*|hook-health*|*-perf|skm-*) return 0 ;; esac
  # Mint exactly once per session id (marker independent of timer-file ordering).
  local marker="$TIMER_DIR/${SESSION_ID}.skm-minted"
  [ -f "$marker" ] && return 0
  : > "$marker" 2>/dev/null || return 0
  local ttl=5400                                   # orch: 90 min (hard-block is 53)
  [ "$AGENT_NAME" = "meta" ] && ttl=36000          # meta: 10 h (no time limit)
  ( SKM_AGENT="$AGENT_NAME" SKM_DEFAULT_TTL="$ttl" \
      "$HOME/.claude/bin/skm" mint "$SESSION_ID" "$AGENT_NAME" "$ttl" >/dev/null 2>&1 & ) 2>/dev/null
  # Opportunistic reap of dead/expired sessions, piggybacked on a real mint so it shares the
  # same safety gates above (ENABLED, non-synthetic SESSION_ID) and never blocks session start.
  ( "$HOME/.claude/bin/skm" gc >/dev/null 2>&1 & ) 2>/dev/null
}
