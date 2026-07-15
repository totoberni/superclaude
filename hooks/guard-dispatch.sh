#!/usr/bin/env bash
# ~/.claude/hooks/guard-dispatch.sh — PreToolUse guard dispatcher (PHASE2-CONTRACT sec 5).
#
# Thin dispatcher mirroring session-timer.sh: source lib.sh, source the guard lib +
# every numbered guard, guard_init, then explicit ordered dispatch_guard calls (each
# runs its guard in an isolated subshell; see dispatch_guard below). A guard blocks
# by exit 2 propagating out; a clean run exits 0.
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

# dispatch_guard <fn>: run one guard in an isolated subshell so a RUNTIME abort
# inside it (a set -u unbound-variable reference, a stray exit N) cannot skip
# the guards ordered after it -- the subshell's own abort exit code is caught
# here and swallowed, letting the parent continue. Only an intentional
# guard_block (exit 2) is allowed to propagate out and actually block the
# tool. Without this isolation a fault in an early guard silently disabled
# every downstream guard, including the security-critical git_policy BLOCK
# (SEAL-A-verdict.md M1).
dispatch_guard() {
  ( run_guard "$1" )
  [ "$?" -eq 2 ] && exit 2
  return 0
}

# Explicit ordered PreToolUse guard invocations. Hook/glob order is not a
# contract (see class/feedback: numeric prefix is cosmetic), so each guard is
# called by name; dispatch_guard no-ops any name whose function is not yet
# defined (run_guard's own contract). BLOCK guards run first, defense in
# depth: even with the abort isolation above, a fault in a later WARN/
# heuristic guard must never have a chance to precede a security-critical
# BLOCK guard (SEAL-A-verdict.md M1).
dispatch_guard guard_git_policy
dispatch_guard guard_write_acl
dispatch_guard guard_content_scan
dispatch_guard guard_commit_gate
# Wave 1 WARN/heuristic guards + Wave 2 guards.
dispatch_guard guard_git_verb
dispatch_guard guard_heuristics
dispatch_guard guard_review_dispatch
dispatch_guard guard_seal_binding
dispatch_guard guard_wrong_tool

exit 0
