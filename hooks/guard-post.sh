#!/usr/bin/env bash
# ~/.claude/hooks/guard-post.sh — PostToolUse guard dispatcher (PHASE2-CONTRACT sec 5).
#
# Same skeleton as guard-dispatch.sh but runs guardpost_* functions. PostToolUse
# CANNOT block a tool that already ran; a post-guard flags/injects context only.
# For safety, GUARD_PHASE=post degrades any accidental guard_block to a warn (see
# lib-guard.sh guard_block), so a mis-authored post-guard can never exit 2 here.
#
# INERT until settings.json references it (owner-run apply-script). FAIL-OPEN.
#
# Exit code: always 0 (PostToolUse has no blocking semantics).

set -uo pipefail

INPUT=$(cat)
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
GUARD_PHASE="post"

# Shared helpers (walk_to_agent, get_bash_cmd, safe_int).
. "$HOOK_DIR/lib.sh" 2>/dev/null \
  || printf 'GUARD-WARN [guard-post]: lib.sh not found at %s/lib.sh\n' "$HOOK_DIR" >&2

# Guard foundation (guard_init, guard_mode, guard_block/guard_warn, run_guard).
if ! . "$HOOK_DIR/guards/lib-guard.sh" 2>/dev/null; then
  printf 'GUARD-WARN [guard-post]: lib-guard.sh missing; guards disabled (fail-open)\n' >&2
  exit 0
fi

# Source every numbered guard (defines functions only; inert).
LC_COLLATE=C
for g in "$HOOK_DIR/guards/"[0-9]*.sh; do
  if [ -f "$g" ]; then
    . "$g" 2>/dev/null \
      || printf 'GUARD-WARN [guard-post]: failed to source %s\n' "$g" >&2
  else
    printf 'GUARD-WARN [guard-post]: no guard files matched %s/guards/[0-9]*.sh\n' "$HOOK_DIR" >&2
  fi
done

# Global kill-switch: SUPERCLAUDE_GUARDS=off disables everything.
if guard_kill_switch; then exit 0; fi

guard_init "$INPUT"

# Explicit ordered PostToolUse guard invocations. Wave 2 appends its guardpost_*
# calls below (verdict_shape, seal_binding, wrong_tool, worker_verify). None
# exist yet; run_guard no-ops undefined names.
run_guard guardpost_canary
# Wave 1 PostToolUse arm.
run_guard guardpost_heuristics
# Wave 2 PostToolUse arms.
run_guard guardpost_verdict_shape
run_guard guardpost_seal_binding
run_guard guardpost_seal_binding_void
run_guard guardpost_wrong_tool
run_guard guardpost_worker_verify

exit 0
