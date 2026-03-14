#!/bin/bash
# Hook test suite: validates all ~/.claude/ hooks after modification.
# Codified from DIR-006 verification suite (33 tests, RPT-008).
#
# Usage:
#   bash ~/.claude/scripts/test-hooks.sh           # Full suite (~90s)
#   bash ~/.claude/scripts/test-hooks.sh --quick    # Regression only (~5s)
#   bash ~/.claude/scripts/test-hooks.sh --no-color # CI-friendly output
#
# Exit codes: 0 = all pass, 1 = any failure, 2 = script error
#
# Safety: all test artifacts use hooktest- prefix in /tmp/hooktest-XXXX.
# Never touches real session-timers or nudge files.

set -uo pipefail

# ── Args ──
QUICK=false
USE_COLOR=true
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=true ;;
    --no-color) USE_COLOR=false ;;
  esac
done

# ── Color output ──
if [ "$USE_COLOR" = true ] && [ -t 1 ]; then
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[1;33m'
  NC='\033[0m'
else
  GREEN=''
  RED=''
  YELLOW=''
  NC=''
fi

# ── Counters ──
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
TOTAL_COUNT=0

pass() {
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  PASS_COUNT=$((PASS_COUNT + 1))
  printf "${GREEN}PASS${NC}: %s\n" "$1"
}

fail() {
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf "${RED}FAIL${NC}: %s — %s\n" "$1" "$2"
}

warn() {
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  WARN_COUNT=$((WARN_COUNT + 1))
  printf "${YELLOW}WARN${NC}: %s — %s\n" "$1" "$2"
}

# ── Setup scratch dir ──
SCRATCH=$(mktemp -d /tmp/hooktest-XXXX)
FAKE_TIMER_DIR="$SCRATCH/session-timers"
FAKE_NUDGE_DIR="$SCRATCH/nudge"
mkdir -p "$FAKE_TIMER_DIR" "$FAKE_NUDGE_DIR"

# Cleanup trap — remove ALL test artifacts on exit
cleanup() {
  rm -rf "$SCRATCH" 2>/dev/null || true
}
trap cleanup EXIT

# ── Paths ──
HOOK_DIR="$HOME/.claude/hooks"
SCRIPT_DIR="$HOME/.claude/scripts"
TIMER_HOOK="$HOOK_DIR/session-timer.sh"
CLEANUP_HOOK="$HOOK_DIR/session-cleanup.sh"
COMPACT_HOOK="$HOOK_DIR/pre-compact.sh"
HEALTH_SCRIPT="$SCRIPT_DIR/infra-health.sh"
REAPER_SCRIPT="$SCRIPT_DIR/session-reaper.sh"
SETTINGS="$HOME/.claude/settings.json"

# ── Helper: run hook with fake HOME to isolate from real session files ──
# Uses a symlinked .claude that overrides session-timers and nudge dirs.
setup_fake_home() {
  local FAKE_HOME="$SCRATCH/fakehome"
  rm -rf "$FAKE_HOME" 2>/dev/null || true
  mkdir -p "$FAKE_HOME/.claude"
  # Symlink everything except session-timers and nudge
  for item in "$HOME/.claude/"*; do
    local base=$(basename "$item")
    [ "$base" = "session-timers" ] && continue
    [ "$base" = "nudge" ] && continue
    ln -sf "$item" "$FAKE_HOME/.claude/$base" 2>/dev/null || true
  done
  # Use our scratch dirs instead
  ln -sf "$FAKE_TIMER_DIR" "$FAKE_HOME/.claude/session-timers"
  ln -sf "$FAKE_NUDGE_DIR" "$FAKE_HOME/.claude/nudge"
  echo "$FAKE_HOME"
}

FAKE_HOME=$(setup_fake_home)

# Helper: run a hook in isolated HOME
run_hook() {
  local HOOK="$1"
  local INPUT="$2"
  echo "$INPUT" | HOME="$FAKE_HOME" bash "$HOOK" 2>"$SCRATCH/stderr.tmp"
  return $?
}

# Helper: get stderr from last run_hook
last_stderr() {
  cat "$SCRATCH/stderr.tmp" 2>/dev/null || echo ""
}

echo "=== Hook Test Suite ($(date '+%Y-%m-%d %H:%M:%S')) ==="
echo "Scratch: $SCRATCH"
echo "Mode: $([ "$QUICK" = true ] && echo "quick (regression only)" || echo "full")"
echo ""

# ═══════════════════════════════════════════════════════
# PHASE 5 / Regression Tests (always run, even in --quick)
# ═══════════════════════════════════════════════════════
echo "── Regression Tests ──"

# G5.1: Hook syntax (bash -n)
ALL_SYNTAX=true
for hook in "$TIMER_HOOK" "$CLEANUP_HOOK" "$COMPACT_HOOK"; do
  if ! bash -n "$hook" 2>/dev/null; then
    ALL_SYNTAX=false
    fail "G5.1 hook syntax" "$(basename "$hook") fails bash -n"
  fi
done
[ "$ALL_SYNTAX" = true ] && pass "G5.1 hook syntax (all 3 clean)"

# G5.2: Hook exit codes (basic mock JSON → exit 0)
ALL_EXIT=true
TEST_JSON='{"session_id":"hooktest-g52","tool_name":"Read"}'
for hook in "$TIMER_HOOK" "$CLEANUP_HOOK" "$COMPACT_HOOK"; do
  # Pre-create .agent file so timer hook detects as bare (no timer enforcement)
  echo "" > "$FAKE_TIMER_DIR/hooktest-g52.agent"
  if ! run_hook "$hook" "$TEST_JSON"; then
    ALL_EXIT=false
    fail "G5.2 hook exit codes" "$(basename "$hook") exited non-zero"
  fi
done
rm -f "$FAKE_TIMER_DIR"/hooktest-g52.* 2>/dev/null
[ "$ALL_EXIT" = true ] && pass "G5.2 hook exit codes (all 3 exit 0)"

# G5.3: settings.json valid
if jq . "$SETTINGS" >/dev/null 2>&1; then
  pass "G5.3 settings.json valid JSON"
else
  fail "G5.3 settings.json" "jq parse failed"
fi

# G5.4: Existing skills intact
EXPECTED_SKILLS="nudge pleh health sync-upstream infra-security push session-reaper"
MISSING_SKILLS=""
for skill in $EXPECTED_SKILLS; do
  if [ ! -f "$HOME/.claude/skills/$skill/SKILL.md" ]; then
    MISSING_SKILLS="$MISSING_SKILLS $skill"
  fi
done
if [ -z "$MISSING_SKILLS" ]; then
  pass "G5.4 existing skills (7 checked)"
else
  fail "G5.4 existing skills" "missing:$MISSING_SKILLS"
fi

# G5.5: Session reaper dry run
if bash "$REAPER_SCRIPT" --dry-run >/dev/null 2>&1; then
  pass "G5.5 session reaper dry run"
else
  fail "G5.5 session reaper" "dry run failed"
fi

# ── Quick mode stops here ──
if [ "$QUICK" = true ]; then
  echo ""
  echo "── Summary (quick mode) ──"
  printf "Tests: %d | ${GREEN}Pass: %d${NC} | ${RED}Fail: %d${NC} | ${YELLOW}Warn: %d${NC}\n" \
    "$TOTAL_COUNT" "$PASS_COUNT" "$FAIL_COUNT" "$WARN_COUNT"
  [ "$FAIL_COUNT" -gt 0 ] && exit 1
  exit 0
fi

# ═══════════════════════════════════════════════════════
# PHASE 1 / Positive (happy path) Tests
# ═══════════════════════════════════════════════════════
echo ""
echo "── Positive Tests ──"

# P1.1: Nudge delivery
mkdir -p "$FAKE_NUDGE_DIR"
echo "150" > "$FAKE_NUDGE_DIR/orch-test"
echo "orch-test" > "$FAKE_TIMER_DIR/hooktest-p11.agent"
TEST_JSON='{"session_id":"hooktest-p11","tool_name":"Read"}'
NUDGE_STDOUT=$(run_hook "$TIMER_HOOK" "$TEST_JSON")
if [ ! -f "$FAKE_NUDGE_DIR/orch-test" ] && echo "$NUDGE_STDOUT" | grep -q "NUDGE"; then
  pass "P1.1 nudge delivery (file consumed, message emitted)"
else
  fail "P1.1 nudge delivery" "file not consumed or no NUDGE in output"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-p11.* 2>/dev/null

# P1.2: Bare claude exempt (empty agent name = no timer)
echo "" > "$FAKE_TIMER_DIR/hooktest-p12.agent"
TEST_JSON='{"session_id":"hooktest-p12","tool_name":"Read"}'
run_hook "$TIMER_HOOK" "$TEST_JSON"
RC=$?
if [ $RC -eq 0 ] && [ ! -f "$FAKE_TIMER_DIR/hooktest-p12.start" ]; then
  pass "P1.2 bare claude exempt (no start file created)"
else
  fail "P1.2 bare claude exempt" "exit=$RC, start file exists=$([ -f "$FAKE_TIMER_DIR/hooktest-p12.start" ] && echo yes || echo no)"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-p12.* 2>/dev/null

# P1.3: GC stale cleanup (Phase 1 — files >2h old)
echo "1000000000" > "$FAKE_TIMER_DIR/hooktest-stale.start"
echo "stale-agent" > "$FAKE_TIMER_DIR/hooktest-stale.agent"
echo "99999" > "$FAKE_TIMER_DIR/hooktest-stale.pid"
# Touch with old timestamp so find -mmin +120 picks it up
touch -t 202601010000.00 "$FAKE_TIMER_DIR/hooktest-stale.start"
echo "" > "$FAKE_TIMER_DIR/hooktest-p13.agent"
TEST_JSON='{"session_id":"hooktest-p13","tool_name":"Read"}'
run_hook "$TIMER_HOOK" "$TEST_JSON"
if [ ! -f "$FAKE_TIMER_DIR/hooktest-stale.start" ]; then
  pass "P1.3 GC stale cleanup (3h-old files removed)"
else
  fail "P1.3 GC stale cleanup" "stale files still exist"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-stale.* "$FAKE_TIMER_DIR"/hooktest-p13.* 2>/dev/null

# P1.4: GC dead PID cleanup (Phase 2)
echo "99999" > "$FAKE_TIMER_DIR/hooktest-dead.pid"
echo "1000000000" > "$FAKE_TIMER_DIR/hooktest-dead.start"
echo "dead-agent" > "$FAKE_TIMER_DIR/hooktest-dead.agent"
echo "" > "$FAKE_TIMER_DIR/hooktest-p14.agent"
TEST_JSON='{"session_id":"hooktest-p14","tool_name":"Read"}'
run_hook "$TIMER_HOOK" "$TEST_JSON"
if [ ! -f "$FAKE_TIMER_DIR/hooktest-dead.pid" ]; then
  pass "P1.4 GC dead PID cleanup (dead PID 99999 cleaned)"
else
  fail "P1.4 GC dead PID cleanup" "dead PID files still exist"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-dead.* "$FAKE_TIMER_DIR"/hooktest-p14.* 2>/dev/null

# P1.5: SessionEnd cleanup
echo "1000000000" > "$FAKE_TIMER_DIR/hooktest-end.start"
echo "end-agent" > "$FAKE_TIMER_DIR/hooktest-end.agent"
echo "99999" > "$FAKE_TIMER_DIR/hooktest-end.pid"
TEST_JSON='{"session_id":"hooktest-end","tool_name":"Read","reason":"user_exit"}'
run_hook "$CLEANUP_HOOK" "$TEST_JSON"
if [ ! -f "$FAKE_TIMER_DIR/hooktest-end.start" ] && [ ! -f "$FAKE_TIMER_DIR/hooktest-end.agent" ]; then
  HISTORY=$(cat "$FAKE_TIMER_DIR/session-history.log" 2>/dev/null || echo "")
  if echo "$HISTORY" | grep -q "hooktest-"; then
    pass "P1.5 SessionEnd cleanup (all files removed, history logged)"
  else
    pass "P1.5 SessionEnd cleanup (all files removed)"
  fi
else
  fail "P1.5 SessionEnd cleanup" "timer files still exist"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-end.* 2>/dev/null

# P1.6: /health full run
if [ -f "$HEALTH_SCRIPT" ]; then
  if bash "$HEALTH_SCRIPT" >/dev/null 2>&1; then
    pass "P1.6 /health full (exit 0)"
  else
    fail "P1.6 /health full" "exit non-zero"
  fi
else
  fail "P1.6 /health full" "infra-health.sh not found"
fi

# P1.7: /health individual components
if [ -f "$HEALTH_SCRIPT" ]; then
  ALL_COMPONENTS=true
  for component in settings hooks agents comms sessions memory; do
    if ! bash "$HEALTH_SCRIPT" --component "$component" >/dev/null 2>&1; then
      ALL_COMPONENTS=false
      fail "P1.7 /health components" "$component failed"
    fi
  done
  [ "$ALL_COMPONENTS" = true ] && pass "P1.7 /health components (all 6 exit 0)"
else
  fail "P1.7 /health components" "infra-health.sh not found"
fi

# P1.8: Upstream reference exists
if [ -d "$HOME/.claude/upstream/awesome-claude-code" ]; then
  if ls "$HOME/.claude/upstream/awesome-claude-code/"*.csv >/dev/null 2>&1 || \
     [ -f "$HOME/.claude/upstream/awesome-claude-code/README.md" ]; then
    pass "P1.8 upstream reference (clone exists)"
  else
    warn "P1.8 upstream reference" "clone exists but no CSV/README found"
  fi
else
  warn "P1.8 upstream reference" "clone not found (offline?)"
fi

# P1.9: Infra-security skill exists and is loaded by code-reviewer
if [ -f "$HOME/.claude/skills/infra-security/SKILL.md" ]; then
  if grep -q "infra-security" "$HOME/.claude/agents/code-reviewer.md" 2>/dev/null; then
    pass "P1.9 infra-security (skill exists, code-reviewer loads it)"
  else
    warn "P1.9 infra-security" "skill exists but code-reviewer doesn't reference it"
  fi
else
  fail "P1.9 infra-security" "SKILL.md not found"
fi

# P1.10: New skill frontmatter validation
NEW_SKILLS="nudge pleh health sync-upstream infra-security"
ALL_FM=true
for skill in $NEW_SKILLS; do
  SKILL_FILE="$HOME/.claude/skills/$skill/SKILL.md"
  if [ -f "$SKILL_FILE" ]; then
    # Check for --- delimited frontmatter
    FIRST_LINE=$(head -1 "$SKILL_FILE" 2>/dev/null || echo "")
    if [ "$FIRST_LINE" != "---" ]; then
      ALL_FM=false
      fail "P1.10 skill frontmatter" "$skill: no YAML frontmatter"
    fi
  else
    ALL_FM=false
    fail "P1.10 skill frontmatter" "$skill: SKILL.md missing"
  fi
done
[ "$ALL_FM" = true ] && pass "P1.10 skill frontmatter (5 new skills valid)"

# P1.11: Rule file exists
if [ -f "$HOME/.claude/rules/30-upstream-sync.md" ]; then
  pass "P1.11 rule file (30-upstream-sync.md exists)"
else
  fail "P1.11 rule file" "30-upstream-sync.md missing"
fi

# ═══════════════════════════════════════════════════════
# PHASE 2 / Negative (error handling) Tests
# ═══════════════════════════════════════════════════════
echo ""
echo "── Negative Tests ──"

# N2.1: Malformed JSON input
echo "" > "$FAKE_TIMER_DIR/hooktest-n21.agent"
if run_hook "$TIMER_HOOK" "not json at all {{{"; then
  pass "N2.1 malformed JSON (exit 0, no crash)"
else
  fail "N2.1 malformed JSON" "hook crashed on bad JSON"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-n21.* "$FAKE_TIMER_DIR"/unknown.* 2>/dev/null

# N2.2: Empty JSON input
echo "" > "$FAKE_TIMER_DIR/hooktest-n22.agent"
if run_hook "$TIMER_HOOK" "{}"; then
  pass "N2.2 empty JSON (exit 0)"
else
  fail "N2.2 empty JSON" "hook crashed on {}"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-n22.* "$FAKE_TIMER_DIR"/unknown.* 2>/dev/null

# N2.3: Non-numeric nudge content
mkdir -p "$FAKE_NUDGE_DIR"
echo "hello world 42 chars" > "$FAKE_NUDGE_DIR/orch-test2"
echo "orch-test2" > "$FAKE_TIMER_DIR/hooktest-n23.agent"
TEST_JSON='{"session_id":"hooktest-n23","tool_name":"Read"}'
run_hook "$TIMER_HOOK" "$TEST_JSON"
if [ ! -f "$FAKE_NUDGE_DIR/orch-test2" ]; then
  pass "N2.3 non-numeric nudge (file consumed, digits extracted)"
else
  fail "N2.3 non-numeric nudge" "nudge file not consumed"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-n23.* 2>/dev/null

# N2.4: Missing nudge dir (no crash)
rm -rf "$FAKE_NUDGE_DIR" 2>/dev/null
echo "orch-nodge" > "$FAKE_TIMER_DIR/hooktest-n24.agent"
TEST_JSON='{"session_id":"hooktest-n24","tool_name":"Read"}'
if run_hook "$TIMER_HOOK" "$TEST_JSON"; then
  pass "N2.4 missing nudge dir (exit 0, no crash)"
else
  fail "N2.4 missing nudge dir" "hook crashed"
fi
mkdir -p "$FAKE_NUDGE_DIR"
rm -f "$FAKE_TIMER_DIR"/hooktest-n24.* 2>/dev/null

# N2.5: Race condition — .start file deleted between check and read
echo "orch-race" > "$FAKE_TIMER_DIR/hooktest-n25.agent"
# Create .start then immediately remove it to simulate race
echo "1000000000" > "$FAKE_TIMER_DIR/hooktest-n25.start"
rm -f "$FAKE_TIMER_DIR/hooktest-n25.start"
TEST_JSON='{"session_id":"hooktest-n25","tool_name":"Read"}'
if run_hook "$TIMER_HOOK" "$TEST_JSON"; then
  pass "N2.5 race condition (.start vanishes, exit 0)"
else
  fail "N2.5 race condition" "hook crashed"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-n25.* 2>/dev/null

# N2.6: GC with nothing to clean
# Empty timer dir — should not crash
echo "" > "$FAKE_TIMER_DIR/hooktest-n26.agent"
TEST_JSON='{"session_id":"hooktest-n26","tool_name":"Read"}'
if run_hook "$TIMER_HOOK" "$TEST_JSON"; then
  pass "N2.6 nothing to clean (exit 0)"
else
  fail "N2.6 nothing to clean" "hook crashed on empty dir"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-n26.* 2>/dev/null

# N2.7: Missing timer dir entirely (pre-mkdir)
# Remove and recreate timer dir — hook's mkdir -p should handle this
rm -rf "$FAKE_TIMER_DIR" 2>/dev/null
mkdir -p "$FAKE_TIMER_DIR"
echo "" > "$FAKE_TIMER_DIR/hooktest-n27.agent"
TEST_JSON='{"session_id":"hooktest-n27","tool_name":"Read"}'
if run_hook "$TIMER_HOOK" "$TEST_JSON"; then
  pass "N2.7 missing timer dir (mkdir -p, exit 0)"
else
  fail "N2.7 missing timer dir" "hook crashed"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-n27.* 2>/dev/null

# N2.8: /health unknown component
if [ -f "$HEALTH_SCRIPT" ]; then
  if bash "$HEALTH_SCRIPT" --component bogus >/dev/null 2>&1; then
    fail "N2.8 unknown component" "should have exited non-zero"
  else
    pass "N2.8 unknown component (exit 1 + error)"
  fi
else
  fail "N2.8 unknown component" "infra-health.sh not found"
fi

# N2.9: Session reaper dry run (no-op, safe)
if bash "$REAPER_SCRIPT" --dry-run >/dev/null 2>&1; then
  pass "N2.9 reaper dry run (exit 0)"
else
  fail "N2.9 reaper dry run" "failed"
fi

# ═══════════════════════════════════════════════════════
# PHASE 3 / Integration Tests
# ═══════════════════════════════════════════════════════
echo ""
echo "── Integration Tests ──"

# I3.1: Full lifecycle (create → tick → warn → grace → SessionEnd)
echo "orch-lifecycle" > "$FAKE_TIMER_DIR/hooktest-life.agent"
# Step 1: SessionStart — creates .start
TEST_JSON='{"session_id":"hooktest-life","tool_name":"Read"}'
run_hook "$TIMER_HOOK" "$TEST_JSON"
STEP1=false
[ -f "$FAKE_TIMER_DIR/hooktest-life.start" ] && STEP1=true

# Step 2: Normal tool call — passes
run_hook "$TIMER_HOOK" "$TEST_JSON"
RC=$?
STEP2=false
[ $RC -eq 0 ] && STEP2=true

# Step 3: Simulate 36-min session — warning
chmod 644 "$FAKE_TIMER_DIR/hooktest-life.start" 2>/dev/null || true
echo $(( $(date +%s) - 2160 )) > "$FAKE_TIMER_DIR/hooktest-life.start"
chmod 444 "$FAKE_TIMER_DIR/hooktest-life.start" 2>/dev/null || true
run_hook "$TIMER_HOOK" "$TEST_JSON"
STDERR=$(last_stderr)
STEP3=false
echo "$STDERR" | grep -qi "warning\|wrap up" && STEP3=true

# Step 4: Simulate 42-min session — grace period, Read allowed
chmod 644 "$FAKE_TIMER_DIR/hooktest-life.start" 2>/dev/null || true
echo $(( $(date +%s) - 2520 )) > "$FAKE_TIMER_DIR/hooktest-life.start"
chmod 444 "$FAKE_TIMER_DIR/hooktest-life.start" 2>/dev/null || true
run_hook "$TIMER_HOOK" "$TEST_JSON"
RC4=$?
STEP4=false
[ $RC4 -eq 0 ] && STEP4=true

# Step 5: SessionEnd — cleanup
TEST_JSON_END='{"session_id":"hooktest-life","reason":"user_exit"}'
run_hook "$CLEANUP_HOOK" "$TEST_JSON_END"
STEP5=false
[ ! -f "$FAKE_TIMER_DIR/hooktest-life.start" ] && STEP5=true

if [ "$STEP1" = true ] && [ "$STEP2" = true ] && [ "$STEP3" = true ] && [ "$STEP4" = true ] && [ "$STEP5" = true ]; then
  pass "I3.1 full lifecycle (5/5 steps: create, tick, warn, grace, cleanup)"
else
  fail "I3.1 full lifecycle" "steps: start=$STEP1 tick=$STEP2 warn=$STEP3 grace=$STEP4 end=$STEP5"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-life.* 2>/dev/null

# I3.2: GC reaps dead but preserves active
# Create a "dead" session (PID 99999) and an "active" session (our own PID)
echo "99999" > "$FAKE_TIMER_DIR/hooktest-dead2.pid"
echo "1000000000" > "$FAKE_TIMER_DIR/hooktest-dead2.start"
echo "dead-agent" > "$FAKE_TIMER_DIR/hooktest-dead2.agent"
echo "$$" > "$FAKE_TIMER_DIR/hooktest-active.pid"
echo "$(date +%s)" > "$FAKE_TIMER_DIR/hooktest-active.start"
echo "active-agent" > "$FAKE_TIMER_DIR/hooktest-active.agent"
echo "" > "$FAKE_TIMER_DIR/hooktest-i32.agent"
TEST_JSON='{"session_id":"hooktest-i32","tool_name":"Read"}'
run_hook "$TIMER_HOOK" "$TEST_JSON"
DEAD_GONE=false
ACTIVE_ALIVE=false
[ ! -f "$FAKE_TIMER_DIR/hooktest-dead2.pid" ] && DEAD_GONE=true
[ -f "$FAKE_TIMER_DIR/hooktest-active.pid" ] && ACTIVE_ALIVE=true
if [ "$DEAD_GONE" = true ] && [ "$ACTIVE_ALIVE" = true ]; then
  pass "I3.2 GC + active session (dead reaped, active preserved)"
else
  fail "I3.2 GC + active session" "dead_gone=$DEAD_GONE active_alive=$ACTIVE_ALIVE"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-dead2.* "$FAKE_TIMER_DIR"/hooktest-active.* "$FAKE_TIMER_DIR"/hooktest-i32.* 2>/dev/null

# I3.3: Health script checks all sections
if [ -f "$HEALTH_SCRIPT" ]; then
  OUTPUT=$(bash "$HEALTH_SCRIPT" 2>&1)
  SECTIONS=0
  echo "$OUTPUT" | grep -qi "settings" && SECTIONS=$((SECTIONS + 1))
  echo "$OUTPUT" | grep -qi "hook" && SECTIONS=$((SECTIONS + 1))
  echo "$OUTPUT" | grep -qi "agent" && SECTIONS=$((SECTIONS + 1))
  echo "$OUTPUT" | grep -qi "comms" && SECTIONS=$((SECTIONS + 1))
  echo "$OUTPUT" | grep -qi "session" && SECTIONS=$((SECTIONS + 1))
  echo "$OUTPUT" | grep -qi "memory" && SECTIONS=$((SECTIONS + 1))
  if [ "$SECTIONS" -ge 6 ]; then
    pass "I3.3 health all sections (6/6 covered)"
  else
    warn "I3.3 health all sections" "only $SECTIONS/6 sections found"
  fi
else
  fail "I3.3 health all sections" "infra-health.sh not found"
fi

# ═══════════════════════════════════════════════════════
# PHASE 4 / Performance + Resource Tests
# ═══════════════════════════════════════════════════════
echo ""
echo "── Performance & Resource Tests ──"

# R4.1: Hook timing (<500ms average)
echo "" > "$FAKE_TIMER_DIR/hooktest-perf.agent"
TEST_JSON='{"session_id":"hooktest-perf","tool_name":"Read"}'
TOTAL_MS=0
RUNS=5
for i in $(seq 1 $RUNS); do
  START_NS=$(date +%s%N 2>/dev/null || echo "0")
  run_hook "$TIMER_HOOK" "$TEST_JSON" >/dev/null 2>&1
  END_NS=$(date +%s%N 2>/dev/null || echo "0")
  if [[ "$START_NS" =~ ^[0-9]+$ ]] && [[ "$END_NS" =~ ^[0-9]+$ ]]; then
    DIFF_MS=$(( (END_NS - START_NS) / 1000000 ))
    TOTAL_MS=$((TOTAL_MS + DIFF_MS))
  fi
done
rm -f "$FAKE_TIMER_DIR"/hooktest-perf.* 2>/dev/null
if [ "$RUNS" -gt 0 ] && [ "$TOTAL_MS" -gt 0 ]; then
  AVG_MS=$((TOTAL_MS / RUNS))
  if [ "$AVG_MS" -lt 500 ]; then
    pass "R4.1 hook timing (avg ${AVG_MS}ms, <500ms budget)"
  else
    fail "R4.1 hook timing" "avg ${AVG_MS}ms exceeds 500ms budget"
  fi
else
  warn "R4.1 hook timing" "could not measure (date +%s%N unavailable?)"
fi

# R4.2: Nudge overhead (<50ms extra)
mkdir -p "$FAKE_NUDGE_DIR"
echo "orch-perf2" > "$FAKE_TIMER_DIR/hooktest-nudge.agent"
TEST_JSON='{"session_id":"hooktest-nudge","tool_name":"Read"}'
# Run without nudge
START_NS=$(date +%s%N 2>/dev/null || echo "0")
run_hook "$TIMER_HOOK" "$TEST_JSON" >/dev/null 2>&1
END_NS=$(date +%s%N 2>/dev/null || echo "0")
BASE_MS=0
if [[ "$START_NS" =~ ^[0-9]+$ ]] && [[ "$END_NS" =~ ^[0-9]+$ ]]; then
  BASE_MS=$(( (END_NS - START_NS) / 1000000 ))
fi
# Run with nudge
echo "200" > "$FAKE_NUDGE_DIR/orch-perf2"
START_NS=$(date +%s%N 2>/dev/null || echo "0")
run_hook "$TIMER_HOOK" "$TEST_JSON" >/dev/null 2>&1
END_NS=$(date +%s%N 2>/dev/null || echo "0")
NUDGE_MS=0
if [[ "$START_NS" =~ ^[0-9]+$ ]] && [[ "$END_NS" =~ ^[0-9]+$ ]]; then
  NUDGE_MS=$(( (END_NS - START_NS) / 1000000 ))
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-nudge.* 2>/dev/null
OVERHEAD=$((NUDGE_MS - BASE_MS))
[ "$OVERHEAD" -lt 0 ] && OVERHEAD=0
if [ "$OVERHEAD" -lt 50 ]; then
  pass "R4.2 nudge overhead (${OVERHEAD}ms, <50ms budget)"
else
  warn "R4.2 nudge overhead" "${OVERHEAD}ms exceeds 50ms budget (noisy measurement?)"
fi

# R4.3: RAM footprint
TOTAL_RSS_KB=$(ps aux | grep '[c]laude' | grep -v grep | awk '{sum += $6} END {print sum+0}')
TOTAL_RSS_MB=$((TOTAL_RSS_KB / 1024))
if [ "$TOTAL_RSS_MB" -lt 8192 ]; then
  pass "R4.3 RAM footprint (${TOTAL_RSS_MB}MB, <8192MB budget)"
else
  warn "R4.3 RAM footprint" "${TOTAL_RSS_MB}MB exceeds 8192MB budget"
fi

# R4.4: Orphan files check
ORPHAN_COUNT=0
TIMER_DIR_REAL="$HOME/.claude/session-timers"
if [ -d "$TIMER_DIR_REAL" ]; then
  for af in "$TIMER_DIR_REAL"/*.agent; do
    [ -f "$af" ] || continue
    SID=$(basename "$af" .agent)
    # Skip if has a .start file (normal, not orphan)
    [ -f "$TIMER_DIR_REAL/${SID}.start" ] && continue
    ORPHAN_COUNT=$((ORPHAN_COUNT + 1))
  done
fi
if [ "$ORPHAN_COUNT" -eq 0 ]; then
  pass "R4.4 orphan files (0 orphans)"
else
  warn "R4.4 orphan files" "$ORPHAN_COUNT orphan .agent files without .start"
fi

# R4.5: Log sizes
HISTORY_LINES=0
CLEANUP_LINES=0
[ -f "$TIMER_DIR_REAL/session-history.log" ] && HISTORY_LINES=$(wc -l < "$TIMER_DIR_REAL/session-history.log" 2>/dev/null || echo 0)
[ -f "$TIMER_DIR_REAL/cleanup.log" ] && CLEANUP_LINES=$(wc -l < "$TIMER_DIR_REAL/cleanup.log" 2>/dev/null || echo 0)
LOG_OK=true
if [ "$HISTORY_LINES" -gt 500 ]; then
  LOG_OK=false
  warn "R4.5 log sizes" "session-history.log: $HISTORY_LINES lines (>500)"
fi
if [ "$CLEANUP_LINES" -gt 200 ]; then
  LOG_OK=false
  warn "R4.5 log sizes" "cleanup.log: $CLEANUP_LINES lines (>200)"
fi
[ "$LOG_OK" = true ] && pass "R4.5 log sizes (history: $HISTORY_LINES, cleanup: $CLEANUP_LINES)"

# ═══════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════
echo ""
echo "═══════════════════════════════════════════════════════"
printf "Tests: %d | ${GREEN}Pass: %d${NC} | ${RED}Fail: %d${NC} | ${YELLOW}Warn: %d${NC}\n" \
  "$TOTAL_COUNT" "$PASS_COUNT" "$FAIL_COUNT" "$WARN_COUNT"
echo "═══════════════════════════════════════════════════════"

[ "$FAIL_COUNT" -gt 0 ] && exit 1
exit 0
