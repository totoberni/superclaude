#!/usr/bin/env bash
# Bite-test for 64-seal-binding (PHASE2-CONTRACT sec 6+8, enforcement-gap-ledger.md
# Family 5 #22 seal-identity). Self-contained: /tmp registry only, never touches the
# real ~/.claude/comms/_roles registry or a real SendMessage. (#21 revision-binding
# is bite-tested separately in scripts/tests/test-seal-manifest.sh.)
#
# 64-seal-binding.sh is a Wave 2 guard: guard-dispatch.sh / guard-post.sh already
# source it via their guards/[0-9]*.sh glob, but the explicit run_guard lines are not
# yet appended there (task scope forbids editing the dispatchers). This test builds
# ephemeral Pre/Post harnesses under $TMPD that mirror each dispatcher's flow (source
# lib.sh + lib-guard.sh + the guard, guard_init, run_guard) but call the guard's
# entry function explicitly. Discarded with $TMPD; scaffolding, not a subsystem file.
#
# Registry writer (guardpost_seal_binding, PostToolUse on Agent):
#   (r1) w-reviewer spawn        -> a role=reviewer row is written
#   (r2) w-implementer spawn     -> a role=producer row is written
#   (r3) general-purpose spawn   -> NO row written (unknown type, not governed)
# Seal-request guard (guard_seal_binding, PreToolUse on SendMessage):
#   (a) seal-request to a reviewer by full agentId          -> block
#   (b) seal-request to a reviewer by short name            -> block
#   (c) seal-request to a FRESH non-registered target       -> pass
#   (d) NON-seal message to a reviewer                       -> pass
#   (e) seal-request to a PRODUCER (only reviewers policed)  -> pass
#   (f) non-SendMessage tool (Bash) with seal text           -> pass (not policed)
#   (g) mode=warn, seal-request to a reviewer                -> pass + WARN
#   (h) SUPERCLAUDE_GUARDS=off, seal-request to a reviewer   -> silence

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"
HOOKS_DIR="$(cd "$GUARDS_DIR/.." && pwd)"

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/seal-binding-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
fails=0

ROLES_DIR="$TMPD/_roles"
mkdir -p "$ROLES_DIR"
SESSION="sealtest-session"
REGFILE="$ROLES_DIR/$SESSION.jsonl"

export SUPERCLAUDE_ROLES_DIR="$ROLES_DIR"

# ── Ephemeral PostToolUse harness (registry writer) ─────────────────────────
POST_HARNESS="$TMPD/post-harness.sh"
cat > "$POST_HARNESS" <<HARNESSEOF
#!/usr/bin/env bash
set -uo pipefail
INPUT=\$(cat)
GUARD_PHASE="post"
. "$HOOKS_DIR/lib.sh" 2>/dev/null || true
. "$GUARDS_DIR/lib-guard.sh" || { echo "harness: lib-guard.sh missing" >&2; exit 0; }
. "$GUARDS_DIR/64-seal-binding.sh" || { echo "harness: 64-seal-binding.sh missing" >&2; exit 0; }
if [ "\${SUPERCLAUDE_GUARDS:-}" = "off" ]; then exit 0; fi
guard_init "\$INPUT"
run_guard guardpost_seal_binding
exit 0
HARNESSEOF

# ── Ephemeral PreToolUse harness (seal-request guard) ───────────────────────
PRE_HARNESS="$TMPD/pre-harness.sh"
cat > "$PRE_HARNESS" <<HARNESSEOF
#!/usr/bin/env bash
set -uo pipefail
INPUT=\$(cat)
GUARD_PHASE="pre"
. "$HOOKS_DIR/lib.sh" 2>/dev/null || true
. "$GUARDS_DIR/lib-guard.sh" || { echo "harness: lib-guard.sh missing" >&2; exit 0; }
. "$GUARDS_DIR/64-seal-binding.sh" || { echo "harness: 64-seal-binding.sh missing" >&2; exit 0; }
if [ "\${SUPERCLAUDE_GUARDS:-}" = "off" ]; then exit 0; fi
guard_init "\$INPUT"
run_guard guard_seal_binding
exit 0
HARNESSEOF

# ── stdin builders ──────────────────────────────────────────────────────────
mk_spawn() {  # subagent_type agentId
  jq -nc --arg s "$1" --arg a "$2" --arg sid "$SESSION" \
    '{tool_name:"Agent", tool_input:{subagent_type:$s}, tool_response:{agentId:$a}, session_id:$sid}'
}
mk_send() {   # to message  [tool_name=SendMessage]
  local tool="${3:-SendMessage}"
  jq -nc --arg to "$1" --arg m "$2" --arg t "$tool" --arg sid "$SESSION" \
    '{tool_name:$t, tool_input:{to:$to, message:$m}, session_id:$sid}'
}

check() {  # label condition-result(0=pass)
  if [ "$2" -eq 0 ]; then echo "  PASS: $1"; else echo "  FAIL: $1"; fails=$((fails + 1)); fi
}

# run_send <label> <to> <message> <want_rc> <must> <mustnot> [ENV=VAL ...]
run_send() {
  local label="$1" to="$2" msg="$3" want_rc="$4" must="$5" mustnot="$6"; shift 6
  local err="$TMPD/stderr.txt"
  mk_send "$to" "$msg" | ( env SUPERCLAUDE_ROLES_DIR="$ROLES_DIR" "$@" bash "$PRE_HARNESS" >/dev/null 2>"$err" )
  local rc=$? ok=1
  [ "$rc" -eq "$want_rc" ] || { ok=0; echo "    rc=$rc want=$want_rc"; }
  if [ -n "$must" ] && ! grep -q "$must" "$err"; then ok=0; echo "    stderr missing '$must': $(cat "$err")"; fi
  if [ -n "$mustnot" ] && grep -q "$mustnot" "$err"; then ok=0; echo "    stderr matched forbidden '$mustnot': $(cat "$err")"; fi
  check "$label" "$((ok == 1 ? 0 : 1))"
}

echo "=== test-64-seal-binding ==="

# ── Registry writer cases ───────────────────────────────────────────────────
mk_spawn "w-reviewer"    "aRVW-round1-0123456789ab" | bash "$POST_HARNESS" >/dev/null 2>&1
mk_spawn "w-implementer" "aIMPL-1-0123456789ab"     | bash "$POST_HARNESS" >/dev/null 2>&1
mk_spawn "general-purpose" "aGP-1-0123456789ab"     | bash "$POST_HARNESS" >/dev/null 2>&1

if jq -e -s 'any(.[]; .role=="reviewer" and .subagent_type=="w-reviewer" and .name=="RVW-round1")' "$REGFILE" >/dev/null 2>&1; then
  check "(r1) w-reviewer spawn -> role=reviewer row (name derived from agentId)" 0
else
  check "(r1) w-reviewer spawn -> role=reviewer row (name derived from agentId)" 1
  echo "    registry: $(cat "$REGFILE" 2>/dev/null)"
fi

if jq -e -s 'any(.[]; .role=="producer" and .subagent_type=="w-implementer")' "$REGFILE" >/dev/null 2>&1; then
  check "(r2) w-implementer spawn -> role=producer row" 0
else
  check "(r2) w-implementer spawn -> role=producer row" 1
fi

if jq -e -s 'any(.[]; .subagent_type=="general-purpose")' "$REGFILE" >/dev/null 2>&1; then
  check "(r3) general-purpose spawn -> NO row (unknown type skipped)" 1
  echo "    unexpected registry: $(cat "$REGFILE" 2>/dev/null)"
else
  check "(r3) general-purpose spawn -> NO row (unknown type skipped)" 0
fi

# ── Seal-request guard cases ────────────────────────────────────────────────
SEAL_MSG='Great work. Now SEAL this campaign: emit a SEAL: ACCEPTED line.'
SEAL_MSG_PHRASE='please seal this and give me the final audit'
PLAIN_MSG='thanks, please start round 2 on the diff and check the tests'

run_send "(a) seal-request to reviewer by full agentId -> block" \
  "aRVW-round1-0123456789ab" "$SEAL_MSG" 2 "GUARD-BLOCK" ""
run_send "(b) seal-request to reviewer by short name -> block" \
  "RVW-round1" "$SEAL_MSG_PHRASE" 2 "already served as a reviewer" ""
run_send "(c) seal-request to a FRESH non-registered target -> pass" \
  "FRESH-auditor" "$SEAL_MSG" 0 "" "GUARD-BLOCK"
run_send "(d) NON-seal message to reviewer -> pass" \
  "RVW-round1" "$PLAIN_MSG" 0 "" "GUARD-BLOCK"
run_send "(e) seal-request to a PRODUCER -> pass (only reviewers policed)" \
  "IMPL-1" "$SEAL_MSG" 0 "" "GUARD-BLOCK"
# (f) a non-continuation tool (Bash) carrying seal text must never be policed.
{
  err="$TMPD/stderr.txt"
  jq -nc --arg sid "$SESSION" '{tool_name:"Bash", tool_input:{command:"echo now SEAL this"}, session_id:$sid}' \
    | ( env SUPERCLAUDE_ROLES_DIR="$ROLES_DIR" bash "$PRE_HARNESS" >/dev/null 2>"$err" )
  rc=$?
  if [ "$rc" -eq 0 ] && ! grep -q "GUARD-" "$err"; then
    check "(f) Bash tool with seal text -> pass (not a continuation tool)" 0
  else
    check "(f) Bash tool with seal text -> pass (not a continuation tool)" 1
  fi
}
run_send "(g) mode=warn, seal-request to reviewer -> pass + WARN" \
  "RVW-round1" "$SEAL_MSG" 0 "WARN" "GUARD-BLOCK" SUPERCLAUDE_GUARD_SEAL_BINDING=warn
run_send "(h) SUPERCLAUDE_GUARDS=off, seal-request to reviewer -> silence" \
  "RVW-round1" "$SEAL_MSG" 0 "" "GUARD-" SUPERCLAUDE_GUARDS=off

if [ "$fails" -eq 0 ]; then
  echo "test-64-seal-binding: ALL PASS"
  exit 0
else
  echo "test-64-seal-binding: $fails case(s) FAILED"
  exit 1
fi
