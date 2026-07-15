#!/usr/bin/env bash
# ~/.claude/hooks/guard-dispatch.sh — PreToolUse guard dispatcher (PHASE2-CONTRACT sec 5).
#
# Thin dispatcher mirroring session-timer.sh: source lib.sh, source the guard lib +
# every numbered guard, guard_init, then explicit ordered run_guard calls. A guard
# blocks by exit 2 propagating out; a clean run exits 0.
#
# INERT until settings.json references it (the owner-run apply-script wires it).
# FAIL-OPEN: any internal fault WARNs to stderr and passes; only an explicit
# guard_block in block mode blocks.
#
# Exit codes: 0 = allow, 2 = block.

set -uo pipefail

INPUT=$(cat)
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
GUARD_PHASE="pre"

# Shared helpers (walk_to_agent, get_bash_cmd, safe_int).
. "$HOOK_DIR/lib.sh" 2>/dev/null \
  || printf 'GUARD-WARN [guard-dispatch]: lib.sh not found at %s/lib.sh\n' "$HOOK_DIR" >&2

# Guard foundation (guard_init, guard_mode, guard_block/guard_warn, run_guard).
if ! . "$HOOK_DIR/guards/lib-guard.sh" 2>/dev/null; then
  printf 'GUARD-WARN [guard-dispatch]: lib-guard.sh missing; guards disabled (fail-open)\n' >&2
  exit 0
fi

# Source every numbered guard (defines functions only; inert). Warn-not-die on a
# missing/broken file so one bad guard never bricks the dispatcher.
LC_COLLATE=C
for g in "$HOOK_DIR/guards/"[0-9]*.sh; do
  if [ -f "$g" ]; then
    . "$g" 2>/dev/null \
      || printf 'GUARD-WARN [guard-dispatch]: failed to source %s\n' "$g" >&2
  else
    printf 'GUARD-WARN [guard-dispatch]: no guard files matched %s/guards/[0-9]*.sh\n' "$HOOK_DIR" >&2
  fi
done

# Global kill-switch: SUPERCLAUDE_GUARDS=off disables everything.
if guard_kill_switch; then exit 0; fi

guard_init "$INPUT"

# Explicit ordered PreToolUse guard invocations. Hook/glob order is not a
# contract (see class/feedback: numeric prefix is cosmetic), so each guard is
# called by name. run_guard no-ops any name whose function is not yet defined.
run_guard guard_canary
# Wave 1 guards (meta-wired at integration, PHASE2-CONTRACT sec 1 order).
run_guard guard_content_scan
run_guard guard_write_acl
run_guard guard_git_policy
run_guard guard_commit_gate
run_guard guard_git_verb
run_guard guard_heuristics
run_guard guard_review_dispatch
run_guard guard_seal_binding
run_guard guard_wrong_tool
# run_guard no-ops any name whose function is not yet defined (fail-open by design).

exit 0
