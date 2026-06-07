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
# Redirect pre-compact snapshots into SCRATCH so hook tests never pollute the real dir.
export COMPACT_SNAPSHOT_DIR="$SCRATCH/compact-snapshots"
mkdir -p "$COMPACT_SNAPSHOT_DIR"

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

# Helper: source lib.sh + a single module in a subshell.
# Use this in M6 module-unit tests instead of bare `source $HOOK_DIR/modules/...`
# to ensure lib.sh helpers (safe_int, get_bash_cmd, emit_context, etc.) are defined.
# Production session-timer.sh sources lib.sh BEFORE iterating modules; this mirrors that.
source_module() {
  . "$HOOK_DIR/lib.sh"
  source "$HOOK_DIR/modules/$1"
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

# P1.9: Infra-security skill exists and is loaded by w-reviewer
if [ -f "$HOME/.claude/skills/infra-security/SKILL.md" ]; then
  if grep -q "infra-security" "$HOME/.claude/agents/w-reviewer.md" 2>/dev/null; then
    pass "P1.9 infra-security (skill exists, w-reviewer loads it)"
  else
    warn "P1.9 infra-security" "skill exists but w-reviewer doesn't reference it"
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

# Step 3: Simulate 46-min session — warning (threshold: 45 min)
chmod 644 "$FAKE_TIMER_DIR/hooktest-life.start" 2>/dev/null || true
echo $(( $(date +%s) - 2760 )) > "$FAKE_TIMER_DIR/hooktest-life.start"
chmod 444 "$FAKE_TIMER_DIR/hooktest-life.start" 2>/dev/null || true
run_hook "$TIMER_HOOK" "$TEST_JSON"
STDERR=$(last_stderr)
STEP3=false
echo "$STDERR" | grep -qi "warning\|wrap up" && STEP3=true

# Step 4: Simulate 49-min session — grace period (threshold: 48 min), Read allowed
chmod 644 "$FAKE_TIMER_DIR/hooktest-life.start" 2>/dev/null || true
echo $(( $(date +%s) - 2940 )) > "$FAKE_TIMER_DIR/hooktest-life.start"
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
# PHASE 6 / Module Unit Tests
# ═══════════════════════════════════════════════════════
echo ""
echo "── Module Unit Tests ──"

# M6.1: mod_parse extracts SESSION_ID and TOOL_NAME from JSON
(
  INPUT='{"session_id":"test-parse-001","tool_name":"Edit"}'
  TIMER_DIR="$FAKE_TIMER_DIR"
  NUDGE_DIR="$FAKE_NUDGE_DIR"
  NUDGE_FIRED=false
  SESSION_ID=""
  AGENT_NAME=""
  TOOL_NAME=""
  START_FILE=""
  OVERRIDE_FILE=""
  AGENT_FILE=""
  PID_FILE=""
  CLAUDE_PID=""
  source_module 00-parse.sh
  mod_parse
  if [ "$SESSION_ID" = "test-parse-001" ] && [ "$TOOL_NAME" = "Edit" ]; then
    exit 0
  else
    exit 1
  fi
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "M6.1 mod_parse (SESSION_ID + TOOL_NAME extracted)"
else
  fail "M6.1 mod_parse" "SESSION_ID or TOOL_NAME not set correctly"
fi

# M6.2: mod_parse derives START_FILE from session ID
(
  INPUT='{"session_id":"test-derive-002","tool_name":"Read"}'
  TIMER_DIR="$FAKE_TIMER_DIR"
  NUDGE_DIR="$FAKE_NUDGE_DIR"
  NUDGE_FIRED=false
  SESSION_ID=""
  AGENT_NAME=""
  TOOL_NAME=""
  START_FILE=""
  OVERRIDE_FILE=""
  AGENT_FILE=""
  PID_FILE=""
  CLAUDE_PID=""
  source_module 00-parse.sh
  mod_parse
  if [ "$START_FILE" = "$FAKE_TIMER_DIR/test-derive-002.start" ] && \
     [ "$AGENT_FILE" = "$FAKE_TIMER_DIR/test-derive-002.agent" ]; then
    exit 0
  else
    exit 1
  fi
) 2>/dev/null
rm -f "$FAKE_TIMER_DIR"/test-derive-002.* 2>/dev/null
if [ $? -eq 0 ]; then
  pass "M6.2 mod_parse (START_FILE + AGENT_FILE derived)"
else
  fail "M6.2 mod_parse" "derived paths incorrect"
fi

# M6.3: mod_context_check one-shot (creates .context-warned, skips on second call)
(
  SESSION_ID="test-ctx-003"
  AGENT_NAME="orch-test"
  TIMER_DIR="$FAKE_TIMER_DIR"
  echo "2" > "$FAKE_TIMER_DIR/test-ctx-003.calls"
  source_module 05-context-check.sh
  # First call — may or may not fire (depends on memory size) but shouldn't crash
  mod_context_check >/dev/null 2>&1
  exit 0
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "M6.3 mod_context_check (no crash on first call)"
else
  fail "M6.3 mod_context_check" "crashed on first call"
fi
rm -f "$FAKE_TIMER_DIR"/test-ctx-003.* 2>/dev/null

# M6.4: mod_counter increments .calls file
(
  SESSION_ID="test-cnt-004"
  AGENT_NAME="orch-test"
  TIMER_DIR="$FAKE_TIMER_DIR"
  TOOL_NAME="Read"
  NUDGE_FIRED=false
  INPUT='{"tool_input":{}}'
  source_module 20-counter.sh
  mod_counter >/dev/null 2>&1
  C1=$(cat "$FAKE_TIMER_DIR/test-cnt-004.calls" 2>/dev/null | tr -cd '0-9')
  mod_counter >/dev/null 2>&1
  C2=$(cat "$FAKE_TIMER_DIR/test-cnt-004.calls" 2>/dev/null | tr -cd '0-9')
  if [ "$C1" = "1" ] && [ "$C2" = "2" ]; then
    exit 0
  else
    exit 1
  fi
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "M6.4 mod_counter (.calls increments 1→2)"
else
  fail "M6.4 mod_counter" ".calls not incrementing correctly"
fi
rm -f "$FAKE_TIMER_DIR"/test-cnt-004.* 2>/dev/null

# M6.5: mod_counter TDD awareness (Edit increments .tdd, Bash pytest resets)
(
  SESSION_ID="test-tdd-005"
  AGENT_NAME="orch-test"
  TIMER_DIR="$FAKE_TIMER_DIR"
  NUDGE_FIRED=false
  INPUT='{"tool_input":{}}'
  source_module 20-counter.sh
  # Two Edit calls
  TOOL_NAME="Edit"
  mod_counter >/dev/null 2>&1
  mod_counter >/dev/null 2>&1
  T1=$(cat "$FAKE_TIMER_DIR/test-tdd-005.tdd" 2>/dev/null | tr -cd '0-9')
  # Test run resets
  TOOL_NAME="Bash"
  INPUT='{"tool_input":{"command":"pytest tests/"}}'
  mod_counter >/dev/null 2>&1
  T2=$(cat "$FAKE_TIMER_DIR/test-tdd-005.tdd" 2>/dev/null | tr -cd '0-9')
  if [ "$T1" = "2" ] && [ "$T2" = "0" ]; then
    exit 0
  else
    exit 1
  fi
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "M6.5 mod_counter TDD (Edit→2, pytest→0)"
else
  fail "M6.5 mod_counter TDD" ".tdd not tracking correctly"
fi
rm -f "$FAKE_TIMER_DIR"/test-tdd-005.* 2>/dev/null

# M6.6: mod_commit_gate passes conventional format
# Strengthened: capture stderr separately, assert exit 0, assert no "command not found"
# (catches future regressions where lib.sh fails to load and helpers are undefined)
(
  SESSION_ID="test-cg-006"
  TIMER_DIR="$FAKE_TIMER_DIR"
  TOOL_NAME="Bash"
  NUDGE_FIRED=false
  INPUT='{"tool_input":{"command":"git commit -m \"feat: add user login\""}}'
  source_module 25-commit-gate.sh
  mod_commit_gate
) > "$SCRATCH/m66.stdout" 2> "$SCRATCH/m66.stderr"
M66_RC=$?
if [ "$M66_RC" = "0" ] \
   && ! grep -q "command not found" "$SCRATCH/m66.stderr" 2>/dev/null \
   && ! grep -q "may not follow" "$SCRATCH/m66.stdout" 2>/dev/null; then
  pass "M6.6 mod_commit_gate (conventional format passes silently)"
else
  fail "M6.6 mod_commit_gate" "exit=$M66_RC stderr=$(cat "$SCRATCH/m66.stderr" 2>/dev/null)"
fi
rm -f "$SCRATCH/m66.stdout" "$SCRATCH/m66.stderr" "$FAKE_TIMER_DIR"/test-cg-006.* 2>/dev/null

# M6.7: mod_commit_gate warns on non-conventional format
# SESSION_ID required: mod_commit_gate calls already_warned "$SESSION_ID" "commit-gate"
# which would error under `set -u` if SESSION_ID is unset.
(
  SESSION_ID="test-cg-007"
  TIMER_DIR="$FAKE_TIMER_DIR"
  TOOL_NAME="Bash"
  NUDGE_FIRED=false
  INPUT='{"tool_input":{"command":"git commit -m \"fixed some stuff\""}}'
  source_module 25-commit-gate.sh
  OUTPUT=$(mod_commit_gate 2>&1)
  if echo "$OUTPUT" | grep -q "may not follow"; then
    exit 0
  else
    exit 1
  fi
) 2>/dev/null
rm -f "$FAKE_TIMER_DIR"/test-cg-007.* 2>/dev/null
if [ $? -eq 0 ]; then
  pass "M6.7 mod_commit_gate (non-conventional triggers warning)"
else
  fail "M6.7 mod_commit_gate" "no warning on bad commit format"
fi

# M6.8: mod_timer warning at 46min (threshold: 45 min)
echo "orch-timer-008" > "$FAKE_TIMER_DIR/hooktest-m68.agent"
chmod 644 "$FAKE_TIMER_DIR/hooktest-m68.start" 2>/dev/null || true
echo $(( $(date +%s) - 2760 )) > "$FAKE_TIMER_DIR/hooktest-m68.start"
chmod 444 "$FAKE_TIMER_DIR/hooktest-m68.start" 2>/dev/null || true
TEST_JSON='{"session_id":"hooktest-m68","tool_name":"Read"}'
run_hook "$TIMER_HOOK" "$TEST_JSON" >/dev/null
STDERR_M68=$(last_stderr)
if echo "$STDERR_M68" | grep -qi "warning\|wrap up"; then
  pass "M6.8 mod_timer (46min warning message)"
else
  fail "M6.8 mod_timer" "no warning at 46min: $STDERR_M68"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-m68.* 2>/dev/null

# M6.9: mod_timer hard block at 54min (threshold: 53 min)
echo "orch-timer-009" > "$FAKE_TIMER_DIR/hooktest-m69.agent"
chmod 644 "$FAKE_TIMER_DIR/hooktest-m69.start" 2>/dev/null || true
echo $(( $(date +%s) - 3240 )) > "$FAKE_TIMER_DIR/hooktest-m69.start"
chmod 444 "$FAKE_TIMER_DIR/hooktest-m69.start" 2>/dev/null || true
TEST_JSON='{"session_id":"hooktest-m69","tool_name":"Read"}'
run_hook "$TIMER_HOOK" "$TEST_JSON" >/dev/null 2>&1
RC_M69=$?
STDERR_M69=$(last_stderr)
if [ $RC_M69 -eq 2 ] && echo "$STDERR_M69" | grep -qi "hard.*limit\|blocked"; then
  pass "M6.9 mod_timer (54min hard block, exit 2)"
else
  fail "M6.9 mod_timer" "exit=$RC_M69, stderr: $STDERR_M69"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-m69.* 2>/dev/null

# M6.10: mod_gc Phase 3 orphan cleanup (.agent without .start, >2h old)
echo "orphan-agent" > "$FAKE_TIMER_DIR/hooktest-orphan.agent"
touch -t 202601010000.00 "$FAKE_TIMER_DIR/hooktest-orphan.agent"
echo "" > "$FAKE_TIMER_DIR/hooktest-m610.agent"
TEST_JSON='{"session_id":"hooktest-m610","tool_name":"Read"}'
run_hook "$TIMER_HOOK" "$TEST_JSON" >/dev/null 2>&1
if [ ! -f "$FAKE_TIMER_DIR/hooktest-orphan.agent" ]; then
  pass "M6.10 mod_gc Phase 3 (orphan .agent without .start cleaned)"
else
  fail "M6.10 mod_gc Phase 3" "orphan .agent file still exists"
fi
rm -f "$FAKE_TIMER_DIR"/hooktest-orphan.* "$FAKE_TIMER_DIR"/hooktest-m610.* 2>/dev/null

# M6.11: mod_bootstrap freshness warning
(
  AGENT_NAME="scaf"
  SESSION_ID="test-boot-011"
  TIMER_DIR="$FAKE_TIMER_DIR"
  START_FILE="$FAKE_TIMER_DIR/test-boot-011.start"
  # Don't create .start file — bootstrap only fires pre-start
  COMMS_DIR="$SCRATCH/comms-test/scaf"
  mkdir -p "$COMMS_DIR"
  # Create bootstrap.md (old) and directives.md (new)
  echo "old bootstrap" > "$COMMS_DIR/bootstrap.md"
  touch -t 202601010000.00 "$COMMS_DIR/bootstrap.md"
  echo "new directive" > "$COMMS_DIR/directives.md"
  touch -t 202603140000.00 "$COMMS_DIR/directives.md"
  # Override HOME to use our test comms dir
  HOME_ORIG="$HOME"
  HOME="$SCRATCH/comms-fake"
  mkdir -p "$HOME/.claude/comms/scaf"
  echo "old bootstrap" > "$HOME/.claude/comms/scaf/bootstrap.md"
  touch -t 202601010000.00 "$HOME/.claude/comms/scaf/bootstrap.md"
  echo "new directive" > "$HOME/.claude/comms/scaf/directives.md"
  touch -t 202603140000.00 "$HOME/.claude/comms/scaf/directives.md"
  source_module 50-bootstrap.sh
  OUTPUT=$(mod_bootstrap_check 2>&1)
  HOME="$HOME_ORIG"
  if echo "$OUTPUT" | grep -qi "stale\|older"; then
    exit 0
  else
    exit 1
  fi
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "M6.11 mod_bootstrap (stale bootstrap detected)"
else
  fail "M6.11 mod_bootstrap" "no staleness warning when bootstrap.md older than directives.md"
fi
rm -f "$FAKE_TIMER_DIR"/test-boot-011.* 2>/dev/null

# M6.12: mod_nudge fires on valid nudge file (tests 10-nudge module)
mkdir -p "$FAKE_NUDGE_DIR"
(
  AGENT_NAME="orch-nudge-test"
  NUDGE_DIR="$FAKE_NUDGE_DIR"
  NUDGE_FIRED=false
  echo "200" > "$NUDGE_DIR/$AGENT_NAME"
  source_module 10-nudge.sh
  mod_nudge > "$NUDGE_DIR/nudge_output.tmp" 2>&1
  if [ "$NUDGE_FIRED" = true ] && grep -q "NUDGE" "$NUDGE_DIR/nudge_output.tmp" && [ ! -f "$NUDGE_DIR/$AGENT_NAME" ]; then
    exit 0
  else
    exit 1
  fi
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "M6.12 mod_nudge (fires on valid file, 10-nudge)"
else
  fail "M6.12 mod_nudge" "nudge not fired or file not consumed"
fi

# M6.13: mod_nudge no-op without nudge file (tests 10-nudge module)
# Strengthened: capture stderr, assert exit 0 + no "command not found"
# (NUDGE_FIRED=false would also be true if lib.sh failed silently to load)
(
  AGENT_NAME="orch-no-nudge"
  NUDGE_DIR="$FAKE_NUDGE_DIR"
  NUDGE_FIRED=false
  rm -f "$NUDGE_DIR/$AGENT_NAME" 2>/dev/null
  source_module 10-nudge.sh
  mod_nudge
  # Communicate NUDGE_FIRED out of subshell via exit code
  [ "$NUDGE_FIRED" = false ] || exit 99
) > "$SCRATCH/m613.stdout" 2> "$SCRATCH/m613.stderr"
M613_RC=$?
if [ "$M613_RC" = "0" ] \
   && ! grep -q "command not found" "$SCRATCH/m613.stderr" 2>/dev/null; then
  pass "M6.13 mod_nudge (no-op without file, 10-nudge)"
else
  fail "M6.13 mod_nudge" "exit=$M613_RC stderr=$(cat "$SCRATCH/m613.stderr" 2>/dev/null)"
fi
rm -f "$SCRATCH/m613.stdout" "$SCRATCH/m613.stderr" 2>/dev/null

# M6.14: mod_nudge cleans empty nudge file (tests 10-nudge module)
# Strengthened: capture stderr, assert exit 0 + no "command not found"
mkdir -p "$FAKE_NUDGE_DIR"
(
  AGENT_NAME="orch-empty-nudge"
  NUDGE_DIR="$FAKE_NUDGE_DIR"
  NUDGE_FIRED=false
  echo "" > "$NUDGE_DIR/$AGENT_NAME"
  source_module 10-nudge.sh
  mod_nudge
  # Both invariants must hold: file consumed AND nudge not fired
  [ ! -f "$NUDGE_DIR/$AGENT_NAME" ] || exit 98
  [ "$NUDGE_FIRED" = false ] || exit 99
) > "$SCRATCH/m614.stdout" 2> "$SCRATCH/m614.stderr"
M614_RC=$?
if [ "$M614_RC" = "0" ] \
   && ! grep -q "command not found" "$SCRATCH/m614.stderr" 2>/dev/null; then
  pass "M6.14 mod_nudge (cleans empty file, 10-nudge)"
else
  fail "M6.14 mod_nudge" "exit=$M614_RC stderr=$(cat "$SCRATCH/m614.stderr" 2>/dev/null)"
fi
rm -f "$SCRATCH/m614.stdout" "$SCRATCH/m614.stderr" 2>/dev/null

# M6.15: mod_timer creates .start on first call (tests 30-timer module)
rm -f "$FAKE_TIMER_DIR/test-timer-015.start" 2>/dev/null
(
  SESSION_ID="test-timer-015"
  AGENT_NAME="orch-test"
  TIMER_DIR="$FAKE_TIMER_DIR"
  START_FILE="$FAKE_TIMER_DIR/test-timer-015.start"
  OVERRIDE_FILE="$FAKE_TIMER_DIR/test-timer-015.override"
  TOOL_NAME="Read"
  INPUT='{"tool_input":{}}'
  source_module 30-timer.sh
  mod_timer
) 2>/dev/null
if [ -f "$FAKE_TIMER_DIR/test-timer-015.start" ]; then
  pass "M6.15 mod_timer (creates .start on first call, 30-timer)"
else
  fail "M6.15 mod_timer" ".start not created"
fi
rm -f "$FAKE_TIMER_DIR"/test-timer-015.* 2>/dev/null

# M6.16: mod_timer skips enforcement for meta agent (tests 30-timer module)
(
  SESSION_ID="test-timer-016"
  AGENT_NAME="meta"
  TIMER_DIR="$FAKE_TIMER_DIR"
  START_FILE="$FAKE_TIMER_DIR/test-timer-016.start"
  OVERRIDE_FILE="$FAKE_TIMER_DIR/test-timer-016.override"
  TOOL_NAME="Read"
  INPUT='{"tool_input":{}}'
  # Create "very old" start file — would hard-block a non-meta agent
  echo $(( $(date +%s) - 3600 )) > "$START_FILE"
  chmod 444 "$START_FILE" 2>/dev/null || true
  source_module 30-timer.sh
  mod_timer
) 2>/dev/null
RC=$?
if [ $RC -eq 0 ]; then
  pass "M6.16 mod_timer (meta exempt from enforcement, 30-timer)"
else
  fail "M6.16 mod_timer" "meta agent blocked (exit=$RC)"
fi
chmod 644 "$FAKE_TIMER_DIR/test-timer-016.start" 2>/dev/null || true
rm -f "$FAKE_TIMER_DIR"/test-timer-016.* 2>/dev/null

# M6.17: mod_gc Phase 1 removes stale sessions (tests 40-gc module)
(
  TIMER_DIR="$FAKE_TIMER_DIR"
  SESSION_ID="test-gc-017"
  echo "1000000000" > "$TIMER_DIR/test-gc-stale.start"
  echo "stale-agent" > "$TIMER_DIR/test-gc-stale.agent"
  touch -t 202601010000.00 "$TIMER_DIR/test-gc-stale.start"
  source_module 40-gc.sh
  mod_gc
  if [ ! -f "$TIMER_DIR/test-gc-stale.start" ]; then
    exit 0
  else
    exit 1
  fi
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "M6.17 mod_gc Phase 1 (stale session removed, 40-gc)"
else
  fail "M6.17 mod_gc Phase 1" "stale files not cleaned"
fi
rm -f "$FAKE_TIMER_DIR"/test-gc-stale.* "$FAKE_TIMER_DIR"/test-gc-017.* 2>/dev/null

# M6.18: mod_gc Phase 2 cleans dead PIDs (tests 40-gc module)
(
  TIMER_DIR="$FAKE_TIMER_DIR"
  SESSION_ID="test-gc-018"
  echo "99999" > "$TIMER_DIR/test-gc-dead.pid"
  echo "$(date +%s)" > "$TIMER_DIR/test-gc-dead.start"
  echo "dead-agent" > "$TIMER_DIR/test-gc-dead.agent"
  source_module 40-gc.sh
  mod_gc
  if [ ! -f "$TIMER_DIR/test-gc-dead.pid" ]; then
    exit 0
  else
    exit 1
  fi
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "M6.18 mod_gc Phase 2 (dead PID cleaned, 40-gc)"
else
  fail "M6.18 mod_gc Phase 2" "dead PID files not cleaned"
fi
rm -f "$FAKE_TIMER_DIR"/test-gc-dead.* "$FAKE_TIMER_DIR"/test-gc-018.* 2>/dev/null

# ═══════════════════════════════════════════════════════
# PHASE 7 / Standalone-hook + extra-module fail-safe coverage
#   One representative-input test per hook NOT already exercised above.
#   Contract for every test here: the hook must be FAIL-SAFE — exit 0 (or, for
#   the deliberate hard-block guard, the documented exit 2) and emit no traceback.
#   Log-writing hooks run under an ISOLATED $HOME so real telemetry logs
#   (~/.claude/comms/_spawns*.log, _outcomes.log) are never polluted by tests.
# ═══════════════════════════════════════════════════════
echo ""
echo "── Standalone-Hook + Extra-Module Tests ──"

# Isolated, fully-writable HOME for log-emitting standalone hooks.
ISO_HOME="$SCRATCH/isohome"
mkdir -p "$ISO_HOME/.claude/comms" "$ISO_HOME/.claude/session-timers" \
         "$ISO_HOME/.claude/agents/_ephemeral" \
         "$ISO_HOME/.claude/agent-memory/_system/_stop-snapshots" 2>/dev/null

# Helper: run a standalone hook under ISO_HOME, capture rc + stderr.
run_iso_hook() {
  local HOOK="$1" INPUT="$2"
  printf '%s' "$INPUT" | HOME="$ISO_HOME" bash "$HOOK" >"$SCRATCH/iso.stdout" 2>"$SCRATCH/iso.stderr"
  return $?
}
# Assert: exit 0 AND no shell traceback markers in stderr.
iso_failsafe_ok() {
  local rc="$1"
  [ "$rc" = "0" ] || return 1
  grep -qE 'line [0-9]+:|unbound variable|command not found|syntax error' "$SCRATCH/iso.stderr" 2>/dev/null && return 1
  return 0
}

# P7.1: agent-outcome.sh — PostToolUse on Agent return → classifies + logs, exit 0.
AGENT_HOOK="$HOOK_DIR/agent-outcome.sh"
P71_IN='{"tool_name":"Agent","session_id":"hooktest-ao-071","tool_input":{"subagent_type":"w-explorer","description":"recon test"},"tool_response":"## Summary\nFound 3 files. Done."}'
run_iso_hook "$AGENT_HOOK" "$P71_IN"
P71_RC=$?
if iso_failsafe_ok "$P71_RC" && grep -q "w-explorer" "$ISO_HOME/.claude/comms/_outcomes.log" 2>/dev/null; then
  pass "P7.1 agent-outcome.sh (Agent return classified + logged, exit 0)"
else
  fail "P7.1 agent-outcome.sh" "rc=$P71_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.2: agent-outcome.sh — non-Agent tool → silent no-op, exit 0.
run_iso_hook "$AGENT_HOOK" '{"tool_name":"Read","session_id":"hooktest-ao-072"}'
P72_RC=$?
if iso_failsafe_ok "$P72_RC"; then
  pass "P7.2 agent-outcome.sh (non-Agent tool no-op, exit 0)"
else
  fail "P7.2 agent-outcome.sh" "rc=$P72_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.3: subagent-stop.sh — SubagentStop envelope → writes EXIT rows, exit 0.
SUBSTOP_HOOK="$HOOK_DIR/subagent-stop.sh"
P73_IN='{"hook_event_name":"SubagentStop","session_id":"hooktest-ss-073","agent_id":"abc123def","agent_type":"w-implementer","last_assistant_message":"## Verification\nAll tests pass."}'
run_iso_hook "$SUBSTOP_HOOK" "$P73_IN"
P73_RC=$?
if iso_failsafe_ok "$P73_RC" \
   && grep -q "EXIT" "$ISO_HOME/.claude/comms/_spawns.log" 2>/dev/null \
   && grep -q "abc123def" "$ISO_HOME/.claude/comms/_spawns-rich.log" 2>/dev/null; then
  pass "P7.3 subagent-stop.sh (EXIT rows written to both logs, exit 0)"
else
  fail "P7.3 subagent-stop.sh" "rc=$P73_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.4: subagent-stop.sh — empty stdin → fail-safe, exit 0.
run_iso_hook "$SUBSTOP_HOOK" ''
P74_RC=$?
if iso_failsafe_ok "$P74_RC"; then
  pass "P7.4 subagent-stop.sh (empty stdin, exit 0)"
else
  fail "P7.4 subagent-stop.sh" "rc=$P74_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.5: stop.sh — Stop event → snapshots ephemeral state, exit 0.
STOP_HOOK="$HOOK_DIR/stop.sh"
run_iso_hook "$STOP_HOOK" '{"session_id":"hooktest-stop-075","hook_event_name":"Stop"}'
P75_RC=$?
SNAP_MADE=$(ls -d "$ISO_HOME/.claude/agent-memory/_system/_stop-snapshots/hooktest-stop-075-"* 2>/dev/null | head -1)
if iso_failsafe_ok "$P75_RC" && [ -n "$SNAP_MADE" ]; then
  pass "P7.5 stop.sh (snapshot dir created, exit 0)"
else
  fail "P7.5 stop.sh" "rc=$P75_RC snapshot=$SNAP_MADE stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.6: stop.sh — empty session_id → no-op, exit 0.
run_iso_hook "$STOP_HOOK" '{}'
P76_RC=$?
if iso_failsafe_ok "$P76_RC"; then
  pass "P7.6 stop.sh (empty session_id no-op, exit 0)"
else
  fail "P7.6 stop.sh" "rc=$P76_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.7: comms-schema-lint.sh — non-comms file path → silent no-op, exit 0.
LINT_HOOK="$HOOK_DIR/comms-schema-lint.sh"
run_iso_hook "$LINT_HOOK" '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/notes.md"}}'
P77_RC=$?
if iso_failsafe_ok "$P77_RC"; then
  pass "P7.7 comms-schema-lint.sh (non-comms path no-op, exit 0)"
else
  fail "P7.7 comms-schema-lint.sh" "rc=$P77_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.8: comms-schema-lint.sh — well-formed RPT entry in a comms reports.md → exit 0, no warning.
LINT_COMMS_DIR="$ISO_HOME/.claude/comms/orch-test"
mkdir -p "$LINT_COMMS_DIR" 2>/dev/null
printf '## RPT-001\n**Time**: 2026-06-03\n**Directive**: DIR-001\n**Status**: DONE\n' > "$LINT_COMMS_DIR/reports.md"
run_iso_hook "$LINT_HOOK" "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$LINT_COMMS_DIR/reports.md\"}}"
P78_RC=$?
if iso_failsafe_ok "$P78_RC" && ! grep -q "missing required fields" "$SCRATCH/iso.stdout" 2>/dev/null; then
  pass "P7.8 comms-schema-lint.sh (valid RPT entry, no warning, exit 0)"
else
  fail "P7.8 comms-schema-lint.sh" "rc=$P78_RC stdout=$(cat "$SCRATCH/iso.stdout" 2>/dev/null)"
fi

# P7.9: latex-warn.sh — non-.tex edit → silent no-op, exit 0.
LATEX_HOOK="$HOOK_DIR/latex-warn.sh"
run_iso_hook "$LATEX_HOOK" '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/notes.md"}}'
P79_RC=$?
if iso_failsafe_ok "$P79_RC"; then
  pass "P7.9 latex-warn.sh (non-.tex no-op, exit 0)"
else
  fail "P7.9 latex-warn.sh" "rc=$P79_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.10: latex-warn.sh — .tex edit but NO .log present (no build yet) → no-op, exit 0.
LATEX_TEX="$SCRATCH/doc.tex"
printf '\\documentclass{article}\\begin{document}hi\\end{document}\n' > "$LATEX_TEX"
run_iso_hook "$LATEX_HOOK" "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$LATEX_TEX\"}}"
P710_RC=$?
if iso_failsafe_ok "$P710_RC"; then
  pass "P7.10 latex-warn.sh (.tex with no .log, no-op, exit 0)"
else
  fail "P7.10 latex-warn.sh" "rc=$P710_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.11: 14-agent-thinking-nudge.sh — Agent dispatch lacking a thinking keyword
#        for a matrix-prescribed type → emits advisory, returns 0 (never blocks).
(
  TOOL_NAME="Agent"
  SESSION_ID="test-tn-0711"
  TIMER_DIR="$FAKE_TIMER_DIR"
  INPUT='{"tool_input":{"subagent_type":"w-planner","prompt":"Plan the architectural migration across the whole subsystem."}}'
  source_module 14-agent-thinking-nudge.sh
  OUT=$(mod_thinking_nudge 2>&1)
  RC=$?
  [ $RC -eq 0 ] || exit 1
  echo "$OUT" | grep -qi "matrix prescribes" || exit 1
  exit 0
) 2>/dev/null
P711_RC=$?
rm -f "$FAKE_TIMER_DIR"/test-tn-0711.* 2>/dev/null
if [ $P711_RC -eq 0 ]; then
  pass "P7.11 14-agent-thinking-nudge.sh (advisory emitted, returns 0)"
else
  fail "P7.11 14-agent-thinking-nudge.sh" "no advisory or non-zero return (rc=$P711_RC)"
fi

# P7.12: 14-agent-thinking-nudge.sh — non-Agent tool → no-op, returns 0.
(
  TOOL_NAME="Read"
  SESSION_ID="test-tn-0712"
  TIMER_DIR="$FAKE_TIMER_DIR"
  INPUT='{"tool_input":{}}'
  source_module 14-agent-thinking-nudge.sh
  mod_thinking_nudge >/dev/null 2>&1
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "P7.12 14-agent-thinking-nudge.sh (non-Agent no-op, returns 0)"
else
  fail "P7.12 14-agent-thinking-nudge.sh" "non-zero return on non-Agent tool"
fi

# P7.13: 15-baseline-stash.sh — Edit in a NON-/commit-false cwd → sets marker, returns 0.
(
  TOOL_NAME="Edit"
  SESSION_ID="test-bs-0713"
  TIMER_DIR="$FAKE_TIMER_DIR"
  INPUT='{"tool_input":{}}'
  CLAUDE_COMMIT_POLICY="true"
  source_module 15-baseline-stash.sh
  mod_baseline_stash >/dev/null 2>&1
  RC=$?
  [ $RC -eq 0 ] || exit 1
  [ -f "$FAKE_TIMER_DIR/test-bs-0713.baseline-stashed" ] || exit 1
  exit 0
) 2>/dev/null
P713_RC=$?
rm -f "$FAKE_TIMER_DIR"/test-bs-0713.* 2>/dev/null
if [ $P713_RC -eq 0 ]; then
  pass "P7.13 15-baseline-stash.sh (marker set on non-no-commit repo, returns 0)"
else
  fail "P7.13 15-baseline-stash.sh" "marker not set or non-zero return (rc=$P713_RC)"
fi

# P7.14: 15-baseline-stash.sh — non-mutating tool (Read) → no-op, returns 0.
(
  TOOL_NAME="Read"
  SESSION_ID="test-bs-0714"
  TIMER_DIR="$FAKE_TIMER_DIR"
  INPUT='{"tool_input":{}}'
  source_module 15-baseline-stash.sh
  mod_baseline_stash >/dev/null 2>&1
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "P7.14 15-baseline-stash.sh (non-mutating tool no-op, returns 0)"
else
  fail "P7.14 15-baseline-stash.sh" "non-zero return on Read"
fi

# P7.15: 45-spawn-log.sh — Agent dispatch → appends a parent/type/desc row, returns 0.
(
  TOOL_NAME="Agent"
  SESSION_ID="test-sl-0715"
  TIMER_DIR="$FAKE_TIMER_DIR"
  AGENT_NAME="orch-test"
  HOME="$ISO_HOME"
  INPUT='{"tool_input":{"subagent_type":"w-tester","description":"run the suite"}}'
  source_module 45-spawn-log.sh
  mod_spawn_log >/dev/null 2>&1
  RC=$?
  [ $RC -eq 0 ] || exit 1
  grep -q "w-tester" "$ISO_HOME/.claude/comms/_spawns.log" 2>/dev/null || exit 1
  exit 0
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "P7.15 45-spawn-log.sh (spawn row appended, returns 0)"
else
  fail "P7.15 45-spawn-log.sh" "row not appended or non-zero return"
fi

# P7.16: 45-spawn-log.sh — non-Agent tool → no-op, returns 0.
(
  TOOL_NAME="Read"
  SESSION_ID="test-sl-0716"
  TIMER_DIR="$FAKE_TIMER_DIR"
  AGENT_NAME="orch-test"
  HOME="$ISO_HOME"
  INPUT='{"tool_input":{}}'
  source_module 45-spawn-log.sh
  mod_spawn_log >/dev/null 2>&1
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "P7.16 45-spawn-log.sh (non-Agent no-op, returns 0)"
else
  fail "P7.16 45-spawn-log.sh" "non-zero return on non-Agent tool"
fi

# P7.17: 30-notebook-guard.sh — safe tool (Read) → no block, returns 0.
(
  TOOL_NAME="Read"
  INPUT='{"tool_input":{"file_path":"/tmp/foo.py"}}'
  source_module 30-notebook-guard.sh
  mod_notebook_guard >/dev/null 2>&1
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "P7.17 30-notebook-guard.sh (safe tool no block, returns 0)"
else
  fail "P7.17 30-notebook-guard.sh" "blocked a safe Read (non-zero)"
fi

# P7.18: 30-notebook-guard.sh — Edit on .ipynb → HARD BLOCK (exit 2 by design).
#        This is the ONE intentional non-zero exit in the suite; it is the hook's
#        documented job (PreToolUse hard-block). Run in a subshell so exit 2 here
#        does not abort the test runner.
(
  TOOL_NAME="Edit"
  INPUT='{"tool_input":{"file_path":"/tmp/analysis.ipynb"}}'
  source_module 30-notebook-guard.sh
  mod_notebook_guard >/dev/null 2>&1
)
P718_RC=$?
if [ $P718_RC -eq 2 ]; then
  pass "P7.18 30-notebook-guard.sh (.ipynb Edit hard-blocked, exit 2)"
else
  fail "P7.18 30-notebook-guard.sh" "expected exit 2 on .ipynb Edit, got $P718_RC"
fi

# P7.19: hcom-pre-tool-use.sh — PreToolUse on every tool call. MUST be fail-safe
#        (exit 0) regardless of broker/message state — it runs mid-turn and must
#        never abort the CLI. Representative envelope; no agent resolvable here, so
#        it falls through to exit 0 without touching the broker.
HCOM_PRE_HOOK="$HOOK_DIR/hcom-pre-tool-use.sh"
run_iso_hook "$HCOM_PRE_HOOK" '{"session_id":"hooktest-hcompre-0719","tool_name":"Read"}'
P719_RC=$?
if iso_failsafe_ok "$P719_RC"; then
  pass "P7.19 hcom-pre-tool-use.sh (representative input, fail-safe exit 0)"
else
  fail "P7.19 hcom-pre-tool-use.sh" "rc=$P719_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.20: hcom-pre-tool-use.sh — empty stdin → fail-safe, exit 0 (no traceback).
run_iso_hook "$HCOM_PRE_HOOK" ''
P720_RC=$?
if iso_failsafe_ok "$P720_RC"; then
  pass "P7.20 hcom-pre-tool-use.sh (empty stdin, fail-safe exit 0)"
else
  fail "P7.20 hcom-pre-tool-use.sh" "rc=$P720_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.21: hcom-session-end.sh — SessionEnd. MUST be fail-safe (exit 0). No agent
#        resolvable in ISO_HOME → early exit 0 without touching the broker.
HCOM_END_HOOK="$HOOK_DIR/hcom-session-end.sh"
run_iso_hook "$HCOM_END_HOOK" '{"session_id":"hooktest-hcomend-0721","reason":"user_exit"}'
P721_RC=$?
if iso_failsafe_ok "$P721_RC"; then
  pass "P7.21 hcom-session-end.sh (representative input, fail-safe exit 0)"
else
  fail "P7.21 hcom-session-end.sh" "rc=$P721_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.22: hcom-session-end.sh — empty stdin → fail-safe, exit 0 (no traceback).
run_iso_hook "$HCOM_END_HOOK" ''
P722_RC=$?
if iso_failsafe_ok "$P722_RC"; then
  pass "P7.22 hcom-session-end.sh (empty stdin, fail-safe exit 0)"
else
  fail "P7.22 hcom-session-end.sh" "rc=$P722_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.23: env-inject.sh — representative JSON stdin → fail-safe, exit 0.
ENV_INJECT_HOOK="$HOOK_DIR/env-inject.sh"
run_iso_hook "$ENV_INJECT_HOOK" '{"session_id":"hooktest-ei-0723","hook_event_name":"SessionStart"}'
P723_RC=$?
if iso_failsafe_ok "$P723_RC"; then
  pass "P7.23 env-inject.sh (representative input, fail-safe exit 0)"
else
  fail "P7.23 env-inject.sh" "rc=$P723_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.24: env-inject.sh — empty stdin → fail-safe, exit 0 (no traceback).
run_iso_hook "$ENV_INJECT_HOOK" ''
P724_RC=$?
if iso_failsafe_ok "$P724_RC"; then
  pass "P7.24 env-inject.sh (empty stdin, fail-safe exit 0)"
else
  fail "P7.24 env-inject.sh" "rc=$P724_RC stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi

# P7.25: env-inject.sh — CLAUDE_ENV_FILE set → injects HF_HUB_OFFLINE=1, exit 0.
P725_ENV_FILE=$(mktemp)
printf '%s' '{"session_id":"hooktest-ei-0725","hook_event_name":"SessionStart"}' \
  | CLAUDE_ENV_FILE="$P725_ENV_FILE" HOME="$ISO_HOME" bash "$ENV_INJECT_HOOK" \
    >"$SCRATCH/iso.stdout" 2>"$SCRATCH/iso.stderr"
P725_RC=$?
if iso_failsafe_ok "$P725_RC" && grep -qxF "HF_HUB_OFFLINE=1" "$P725_ENV_FILE" 2>/dev/null; then
  pass "P7.25 env-inject.sh (CLAUDE_ENV_FILE populated with HF_HUB_OFFLINE=1, exit 0)"
else
  fail "P7.25 env-inject.sh" "rc=$P725_RC file=$(cat "$P725_ENV_FILE" 2>/dev/null) stderr=$(cat "$SCRATCH/iso.stderr" 2>/dev/null)"
fi
rm -f "$P725_ENV_FILE" 2>/dev/null

rm -f "$SCRATCH/iso.stdout" "$SCRATCH/iso.stderr" 2>/dev/null

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
