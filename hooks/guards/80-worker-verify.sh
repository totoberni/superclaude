# Guard: 80-worker-verify (PostToolUse on Agent). Family 6 #23, R-3 half-mechanization
# (design/enforcement-gap-ledger.md Family 6, rules/40-swarm-quality-gates.md R-3:
# "After every worker returns: 1. Read modified files 2. Run tests if applicable
# 3. git diff --stat 4. Watch for weakened assertions / added skips / scope
# violations 5. If wrong, fix yourself or re-delegate").
#
# MECH-FLAG only (ledger Notes column): a PostToolUse hook can inject the
# verification OBLIGATION onto the spawning agent's next turn, naming the worker
# that just returned; it cannot force the agent to actually read the diff, open
# the modified files, or run the tests. This guard mechanizes only the "the
# obligation is now visible and cannot be silently skipped" half of R-3 -- the
# "read files / run tests" half stays advisory prose. Honest partial.
#
# Deliberately does NOT attempt a live `git diff --stat`: this hook runs inside
# the spawning agent's process, which may span zero, one, or several repos in a
# session, and neither tool_input nor tool_response names which repo the worker
# touched. Guessing a repo path here would be a mirrored, driftable computation
# for a value the spawning agent can already state exactly (it dispatched the
# worker; it knows the repo) -- see rules/20-tool-conventions.md Single Source
# of Truth. The reminder names the command instead of running it.
#
# Fires on EVERY Agent-tool return with a resolvable subagent_type -- all
# worker classes, not just the producer role set 60-verdict-shape.sh
# distinguishes for its own purpose. R-3 says "every worker", not "every
# producer". Never blocks (PostToolUse cannot); always guard_warn. FAIL-OPEN:
# an empty/unresolvable subagent_type returns silently (nothing useful to name).

GUARD_MODE_WORKER_VERIFY=warn

guardpost_worker_verify() {
  guard_kill_switch && return 0
  [ "${GUARD_TOOL:-}" = "Agent" ] || return 0

  local subagent_type
  subagent_type=$(guard_field "subagent_type")
  [ -n "$subagent_type" ] || return 0

  guard_warn "R-3 verification due for '$subagent_type': before trusting its return, run 'git diff --stat', read the files it touched, run tests if applicable, and check for weakened assertions / added skips / scope violations (rules/40-swarm-quality-gates.md R-3). This flag cannot force the read; it only makes the obligation visible."
  return 0
}
