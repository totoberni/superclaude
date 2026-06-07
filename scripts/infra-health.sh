#!/bin/bash
# Infrastructure health check for ~/.claude/
# Usage: bash infra-health.sh [--component settings|hooks|agents|comms|sessions|memory]
# Exit: 0 = all pass/warnings, 1 = any failures

set -uo pipefail

CLAUDE_DIR="$HOME/.claude"
TIMER_DIR="$CLAUDE_DIR/session-timers"
COMPONENT=""
HAS_FAILURE=false
HAS_WARNING=false
PYTHON="$HOME/.claude/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="$(command -v python3 2>/dev/null || true)"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --component) COMPONENT="$2"; shift 2 ;;
    *) COMPONENT="$1"; shift ;;
  esac
done

# ── Output helpers ──
pass() { echo "  ✅ $1"; }
warn() { echo "  ⚠️  $1"; HAS_WARNING=true; }
fail() { echo "  ❌ $1"; HAS_FAILURE=true; }
header() { echo ""; echo "[$1]"; }

# Summary collectors
SUMMARY_SETTINGS=""
SUMMARY_HOOKS=""
SUMMARY_AGENTS=""
SUMMARY_COMMS=""
SUMMARY_SESSIONS=""
SUMMARY_MEMORY=""
SUMMARY_RULES=""

# ── 1. settings.json ──
check_settings() {
  header "settings.json"
  local SETTINGS="$CLAUDE_DIR/settings.json"

  if [ ! -f "$SETTINGS" ]; then
    fail "settings.json not found"
    SUMMARY_SETTINGS="❌ NOT FOUND"
    return
  fi

  # JSON parse check
  if jq . "$SETTINGS" > /dev/null 2>&1; then
    pass "Valid JSON"
  else
    fail "INVALID JSON — all agents broken"
    SUMMARY_SETTINGS="❌ INVALID JSON"
    return
  fi

  # Deny rule count
  local DENY_COUNT
  DENY_COUNT=$(jq '.permissions.deny | length' "$SETTINGS" 2>/dev/null || echo "0")
  if [ "$DENY_COUNT" -ge 5 ]; then
    pass "Deny rules: $DENY_COUNT"
  else
    warn "Deny rules: $DENY_COUNT (< 5 — safety erosion?)"
  fi

  # Duplicate key check
  local DUP_CHECK
  DUP_CHECK=$("$PYTHON" "$HOME/.claude/scripts/settings_dupcheck.py" "$SETTINGS" 2>/dev/null || echo "OK")
  if [ "$DUP_CHECK" = "OK" ]; then
    pass "No duplicate JSON keys"
  else
    warn "$DUP_CHECK"
  fi

  # Hook registration completeness
  local HOOKS_REGISTERED=0
  for event in PreCompact SessionStart PreToolUse SessionEnd; do
    if jq -e ".hooks.$event" "$SETTINGS" > /dev/null 2>&1; then
      HOOKS_REGISTERED=$((HOOKS_REGISTERED + 1))
    fi
  done
  if [ "$HOOKS_REGISTERED" -eq 4 ]; then
    pass "All 4 hook events registered"
  else
    warn "Only $HOOKS_REGISTERED/4 hook events registered"
  fi

  # Allow rule count
  local ALLOW_COUNT
  ALLOW_COUNT=$(jq '.permissions.allow | length' "$SETTINGS" 2>/dev/null || echo "0")
  pass "Allow rules: $ALLOW_COUNT"

  SUMMARY_SETTINGS="✅ Valid JSON, $DENY_COUNT deny rules, $HOOKS_REGISTERED hooks registered"
  [ "$HAS_FAILURE" = true ] && SUMMARY_SETTINGS="❌ settings.json has failures"
}

# ── 2. Hooks ──
check_hooks() {
  header "hooks"
  local HOOK_COUNT=0
  local HOOK_PASS=0

  for hook in "$CLAUDE_DIR"/hooks/*.sh; do
    [ -f "$hook" ] || continue
    HOOK_COUNT=$((HOOK_COUNT + 1))
    local NAME
    NAME=$(basename "$hook")

    # Syntax check
    if bash -n "$hook" 2>/dev/null; then
      pass "$NAME: syntax OK"
    else
      fail "$NAME: syntax error"
      continue
    fi

    # Exit 0 with mock input
    if echo '{"session_id":"health-check","tool_name":"Read"}' | bash "$hook" > /dev/null 2>&1; then
      pass "$NAME: exits 0 with mock input"
      HOOK_PASS=$((HOOK_PASS + 1))
    else
      local EXIT_CODE=$?
      warn "$NAME: exits $EXIT_CODE with mock input"
    fi
    # Clean timer artifacts left by mock hook run
    rm -f "$HOME/.claude/session-timers/health-check".{agent,pid,start} 2>/dev/null

    # Executable check
    if [ -x "$hook" ]; then
      pass "$NAME: executable"
    else
      warn "$NAME: not executable (chmod +x needed)"
    fi

    # Size check
    local LINES
    LINES=$(wc -l < "$hook" 2>/dev/null || echo "0")
    if [ "$LINES" -gt 200 ]; then
      warn "$NAME: $LINES lines (complexity risk for PreToolUse)"
    fi
  done

  if [ "$HOOK_COUNT" -eq 0 ]; then
    warn "No hook scripts found"
    SUMMARY_HOOKS="⚠️  No hooks"
  else
    SUMMARY_HOOKS="✅ hooks ($HOOK_COUNT) | $HOOK_PASS/$HOOK_COUNT pass syntax + exit 0"
    [ "$HOOK_PASS" -lt "$HOOK_COUNT" ] && SUMMARY_HOOKS="⚠️  hooks ($HOOK_COUNT) | $HOOK_PASS/$HOOK_COUNT fully passing"
  fi
}

# ── 3. Agents ──
check_agents() {
  header "agents"
  local AGENT_COUNT=0
  local AGENT_ISSUES=0

  for agent in "$CLAUDE_DIR"/agents/*.md; do
    [ -f "$agent" ] || continue
    AGENT_COUNT=$((AGENT_COUNT + 1))
    local NAME
    NAME=$(basename "$agent" .md)

    # Frontmatter check (--- delimited)
    local FIRST_LINE
    FIRST_LINE=$(head -1 "$agent" 2>/dev/null || echo "")
    if [ "$FIRST_LINE" != "---" ]; then
      fail "$NAME: missing YAML frontmatter"
      AGENT_ISSUES=$((AGENT_ISSUES + 1))
      continue
    fi

    # Required fields (name, model)
    local FM
    FM=$(sed -n '2,/^---$/p' "$agent" 2>/dev/null | head -50)
    local HAS_MODEL=false
    echo "$FM" | grep -q "^model:" && HAS_MODEL=true

    if [ "$HAS_MODEL" = true ]; then
      # Validate model value
      local MODEL_VAL
      MODEL_VAL=$(echo "$FM" | grep "^model:" | head -1 | awk '{print $2}' | tr -d '"')
      case "$MODEL_VAL" in
        opus|sonnet|haiku|opus\[1m\]|sonnet\[1m\]|opus\[200k\]|sonnet\[200k\]) pass "$NAME: model=$MODEL_VAL" ;;
        *) warn "$NAME: unknown model '$MODEL_VAL'" ;;
      esac
    else
      warn "$NAME: missing 'model:' field"
      AGENT_ISSUES=$((AGENT_ISSUES + 1))
    fi
  done

  if [ "$AGENT_COUNT" -eq 0 ]; then
    warn "No agent definitions found"
    SUMMARY_AGENTS="⚠️  No agents"
  elif [ "$AGENT_ISSUES" -gt 0 ]; then
    SUMMARY_AGENTS="⚠️  agents ($AGENT_COUNT) | $AGENT_ISSUES issues"
  else
    SUMMARY_AGENTS="✅ agents ($AGENT_COUNT) | All have valid frontmatter"
  fi
}

# ── 4. Comms ──
check_comms() {
  header "comms"
  local REQUIRED_FILES=("bootstrap.md" "directives.md" "escalations.md" "reports.md")
  local COMMS_COUNT=0
  local COMMS_ISSUES=0

  for dir in "$CLAUDE_DIR"/comms/*/; do
    [ -d "$dir" ] || continue
    local DIR_NAME
    DIR_NAME=$(basename "$dir")
    # Skip special directories (not real comms dirs)
    case "$DIR_NAME" in
      _archive|_template) continue ;;
    esac
    COMMS_COUNT=$((COMMS_COUNT + 1))

    local MISSING=0
    for req in "${REQUIRED_FILES[@]}"; do
      if [ ! -f "$dir/$req" ]; then
        MISSING=$((MISSING + 1))
      fi
    done

    if [ "$MISSING" -eq 0 ]; then
      pass "$DIR_NAME: complete (4 files)"
    else
      warn "$DIR_NAME: missing $MISSING required files"
      COMMS_ISSUES=$((COMMS_ISSUES + 1))
    fi

    # Cross-reference: check for matching agent
    if [ ! -f "$CLAUDE_DIR/agents/$DIR_NAME.md" ]; then
      # Could be a named orch (orch-* or o-*) or special dir — these legitimately have no per-instance agent file
      if ! echo "$DIR_NAME" | grep -qE "^(orch-|o-|scaf|meta)"; then
        warn "$DIR_NAME: no matching agent definition"
      fi
    fi
  done

  # Orphan detection: agent exists but no comms dir
  # Only check meta, orch, and named o-* aliases — workers (w-*) and scaf are spawn-only
  for agent in "$CLAUDE_DIR"/agents/*.md; do
    [ -f "$agent" ] || continue
    local AGENT_NAME
    AGENT_NAME=$(basename "$agent" .md)
    # Skip all workers (w-*), scaf, and other base agents that don't need persistent comms
    case "$AGENT_NAME" in
      w-*|scaf|debugger|merge-resolver|refactorer|code-reviewer|planner|orch) continue ;;
    esac
    if [ ! -d "$CLAUDE_DIR/comms/$AGENT_NAME" ]; then
      warn "$AGENT_NAME: agent exists but no comms dir"
    fi
  done

  if [ "$COMMS_COUNT" -eq 0 ]; then
    warn "No comms directories found"
    SUMMARY_COMMS="⚠️  No comms dirs"
  elif [ "$COMMS_ISSUES" -gt 0 ]; then
    SUMMARY_COMMS="⚠️  comms ($COMMS_COUNT) | $COMMS_ISSUES incomplete"
  else
    SUMMARY_COMMS="✅ comms ($COMMS_COUNT) | All complete (4 files each)"
  fi
}

# ── 5. Sessions ──
check_sessions() {
  header "sessions"

  if [ ! -d "$TIMER_DIR" ]; then
    pass "No session-timers directory (fresh install)"
    SUMMARY_SESSIONS="✅ sessions | No timer dir"
    return
  fi

  # Count active sessions
  local ACTIVE
  ACTIVE=$(ls "$TIMER_DIR"/*.start 2>/dev/null | wc -l)
  pass "Active timer files: $ACTIVE"

  # Stale session check (PID dead but files remain)
  local STALE=0
  for pid_file in "$TIMER_DIR"/*.pid; do
    [ -f "$pid_file" ] || continue
    local PID
    PID=$(cat "$pid_file" 2>/dev/null || echo "")
    if [ -n "$PID" ] && ! kill -0 "$PID" 2>/dev/null; then
      local SID
      SID=$(basename "$pid_file" .pid)
      warn "Stale session: $SID (PID $PID is dead)"
      STALE=$((STALE + 1))
    fi
  done
  [ "$STALE" -eq 0 ] && pass "No stale sessions (all PIDs alive or cleaned)"

  # Orphan files check
  local ORPHANS=0
  for f in "$TIMER_DIR"/*.agent "$TIMER_DIR"/*.pid "$TIMER_DIR"/*.override; do
    [ -f "$f" ] || continue
    local BASE
    BASE=$(basename "$f" | sed 's/\.\(agent\|pid\|override\)$//')
    if [ ! -f "$TIMER_DIR/${BASE}.start" ]; then
      warn "Orphan: $(basename "$f") (no matching .start)"
      ORPHANS=$((ORPHANS + 1))
    fi
  done
  [ "$ORPHANS" -eq 0 ] && pass "No orphaned timer files"

  # Session ages
  for start_file in "$TIMER_DIR"/*.start; do
    [ -f "$start_file" ] || continue
    local SID
    SID=$(basename "$start_file" .start)
    local START_EPOCH
    START_EPOCH=$(cat "$start_file" 2>/dev/null || echo "")
    if [[ "$START_EPOCH" =~ ^[0-9]+$ ]]; then
      local AGE=$(( ($(date +%s) - START_EPOCH) / 60 ))
      local AGENT
      AGENT=$(cat "$TIMER_DIR/${SID}.agent" 2>/dev/null || echo "unknown")
      if [ "$AGE" -gt 48 ]; then
        warn "Session ${SID:0:8} ($AGENT): ${AGE}min — past hard limit"
      elif [ "$AGE" -gt 35 ]; then
        warn "Session ${SID:0:8} ($AGENT): ${AGE}min — past warning"
      else
        pass "Session ${SID:0:8} ($AGENT): ${AGE}min"
      fi
    fi
  done

  # RAM check
  local CLAUDE_RSS
  CLAUDE_RSS=$(ps aux 2>/dev/null | grep '[c]laude' | awk '{sum+=$6} END {printf "%.0f", sum/1024}')
  [ -z "$CLAUDE_RSS" ] && CLAUDE_RSS="0"
  if [ "$CLAUDE_RSS" -lt 8192 ]; then
    pass "Claude RSS: ${CLAUDE_RSS}MB (under 8GB budget)"
  else
    warn "Claude RSS: ${CLAUDE_RSS}MB (OVER 8GB budget)"
  fi

  # Timer bypass check: named agent running without .start file
  for agent_file in "$TIMER_DIR"/*.agent; do
    [ -f "$agent_file" ] || continue
    local SID
    SID=$(basename "$agent_file" .agent)
    local AGENT
    AGENT=$(cat "$agent_file" 2>/dev/null || echo "")
    if [ -n "$AGENT" ] && [ "$AGENT" != "meta" ] && [ ! -f "$TIMER_DIR/${SID}.start" ]; then
      warn "Timer bypass: $AGENT (session ${SID:0:8}) has no .start file"
    fi
  done

  # History/cleanup log sizes
  local HIST_LINES=0
  local CLEAN_LINES=0
  [ -f "$TIMER_DIR/session-history.log" ] && HIST_LINES=$(wc -l < "$TIMER_DIR/session-history.log" 2>/dev/null || echo "0")
  [ -f "$TIMER_DIR/cleanup.log" ] && CLEAN_LINES=$(wc -l < "$TIMER_DIR/cleanup.log" 2>/dev/null || echo "0")
  pass "History log: $HIST_LINES lines, Cleanup log: $CLEAN_LINES lines"

  SUMMARY_SESSIONS="✅ sessions ($ACTIVE active) | RSS: ${CLAUDE_RSS}MB"
  [ "$STALE" -gt 0 ] && SUMMARY_SESSIONS="⚠️  sessions ($ACTIVE active, $STALE stale)"
}

# ── 6. Memory ──
# v3 REWRITE (DB-aware): memory is the hybrid-search SQLite store at
# agent-memory/.memory.db, NOT a tree of MEMORY.md/ltm.md/mtm.md files with per-file
# LINE budgets. This check now validates DB INTEGRITY + STATS instead of file
# line-counts:
#   - DB presence + valid-SQLite + required tables (memories, *_fts, *_vec, *_docsize)
#   - row count (corpus population)
#   - FTS index cohesion : memories == memories_fts_docsize (the falsifiable check;
#                          memories_fts is external-content so its COUNT is vacuous)
#   - vec presence       : memories_vec is a vec0 VIRTUAL table — plain sqlite3 cannot
#                          COUNT(*) it, so PRESENCE via sqlite_master is the fail-safe
#                          check (mirrors scripts/super-health.sh).
#   - last-write sanity  : max(updated) timestamp (is the store being written to?)
#   - on-disk footprint  : du -h of the .db file
# Fast path only: bash + sqlite3 CLI, NO python, NO embedding model. Missing sqlite3 /
# missing DB degrade to a warn (not a fail) so this stays usable on pre-DB systems.
check_memory() {
  header "memory"
  local MEM_DIR="$CLAUDE_DIR/agent-memory"
  local MDB="${MEMORY_DB_PATH:-$MEM_DIR/.memory.db}"

  if [ ! -d "$MEM_DIR" ]; then
    warn "No agent-memory directory"
    SUMMARY_MEMORY="⚠️  No memory dir"
    return
  fi

  local MEM_ISSUES=0

  # ── DB availability ──
  if ! command -v sqlite3 >/dev/null 2>&1; then
    warn "sqlite3 absent — cannot inspect memory DB"
    SUMMARY_MEMORY="⚠️  memory | sqlite3 absent"
    return
  fi
  if [ ! -f "$MDB" ]; then
    warn "memory DB not built ($MDB)"
    SUMMARY_MEMORY="⚠️  memory | DB not built"
    return
  fi
  if ! sqlite3 "$MDB" "SELECT 1;" >/dev/null 2>&1; then
    fail "memory DB is not valid SQLite ($MDB)"
    SUMMARY_MEMORY="❌ memory | invalid SQLite"
    return
  fi

  # ── Required tables (core + FTS doc-index shadow + vec0 virtual table) ──
  local TABLES MISS=""
  TABLES=$(sqlite3 "$MDB" "SELECT name FROM sqlite_master WHERE type IN ('table','view');" 2>/dev/null)
  local t
  for t in memories memories_fts memories_vec memories_fts_docsize; do
    printf '%s\n' "$TABLES" | grep -qx "$t" || MISS="$MISS $t"
  done
  if [ -n "$MISS" ]; then
    fail "memory DB missing table(s):$MISS — rebuild via memory_db.py init"
    MEM_ISSUES=$((MEM_ISSUES + 1))
  else
    pass "memory DB schema: core + fts + vec + docsize present"
  fi

  # ── Row count (corpus population) ──
  local ROWS
  ROWS=$(sqlite3 "$MDB" "SELECT COUNT(*) FROM memories;" 2>/dev/null)
  [[ "$ROWS" =~ ^[0-9]+$ ]] || ROWS=0
  if [ "$ROWS" -le 0 ]; then
    warn "memory DB has 0 rows — store is empty"
    MEM_ISSUES=$((MEM_ISSUES + 1))
  else
    pass "memory rows: $ROWS"
  fi

  # ── FTS index cohesion: memories == memories_fts_docsize (falsifiable check) ──
  local IDX
  IDX=$(sqlite3 "$MDB" "SELECT COUNT(*) FROM memories_fts_docsize;" 2>/dev/null)
  [[ "$IDX" =~ ^[0-9]+$ ]] || IDX=0
  if [ "$ROWS" -gt 0 ] && [ "$IDX" -eq "$ROWS" ]; then
    pass "FTS index aligned: fts_idx=$IDX == rows=$ROWS"
  else
    fail "FTS index DESYNC: fts_idx=$IDX != rows=$ROWS — rebuild via memory_db.py init"
    MEM_ISSUES=$((MEM_ISSUES + 1))
  fi

  # ── vec presence (vec0 virtual table; cannot COUNT(*) via plain sqlite3) ──
  local VEC
  VEC=$(sqlite3 "$MDB" "SELECT COUNT(*) FROM sqlite_master WHERE name='memories_vec';" 2>/dev/null)
  [[ "$VEC" =~ ^[0-9]+$ ]] || VEC=0
  if [ "$VEC" -ge 1 ]; then
    pass "vector store present (memories_vec); full triple via memory_db.py stats"
  else
    fail "memories_vec MISSING — vector search unavailable"
    MEM_ISSUES=$((MEM_ISSUES + 1))
  fi

  # ── Per-tier population (replaces per-cell line-budget rows) ──
  local TIER_SUMMARY
  TIER_SUMMARY=$(sqlite3 "$MDB" "SELECT tier || '=' || COUNT(*) FROM memories GROUP BY tier ORDER BY COUNT(*) DESC;" 2>/dev/null | tr '\n' ' ')
  [ -n "$TIER_SUMMARY" ] && pass "tiers: $TIER_SUMMARY"

  # ── Last-write sanity: is the store actually being written to? ──
  local LAST_WRITE
  LAST_WRITE=$(sqlite3 "$MDB" "SELECT COALESCE(MAX(updated),'never') FROM memories;" 2>/dev/null)
  pass "last write: ${LAST_WRITE:-unknown}"

  # ── On-disk footprint of the DB file ──
  local FOOTPRINT
  FOOTPRINT=$(du -h "$MDB" 2>/dev/null | awk '{print $1}')
  pass "memory DB footprint: ${FOOTPRINT:-unknown}"

  if [ "$MEM_ISSUES" -gt 0 ]; then
    SUMMARY_MEMORY="⚠️  memory (DB) | $MEM_ISSUES issue(s), rows=$ROWS"
  else
    SUMMARY_MEMORY="✅ memory (DB) | rows=$ROWS, fts ok, vec ok, ${FOOTPRINT:-?}"
  fi
}

# ── 7. Rules ──
check_rules() {
  header "rules"
  local RULES_DIR="$CLAUDE_DIR/rules"
  local RULE_COUNT=0
  local RULE_ISSUES=0

  if [ ! -d "$RULES_DIR" ]; then
    warn "No rules directory"
    SUMMARY_RULES="⚠️  No rules dir"
    return
  fi

  for rule in "$RULES_DIR"/*.md; do
    [ -f "$rule" ] || continue
    RULE_COUNT=$((RULE_COUNT + 1))
    local NAME
    NAME=$(basename "$rule")

    # Check for paths: frontmatter field
    local FIRST_LINE
    FIRST_LINE=$(head -1 "$rule" 2>/dev/null || echo "")
    if [ "$FIRST_LINE" = "---" ]; then
      # Extract frontmatter (between first and second ---)
      local FM
      FM=$(sed -n '2,/^---$/p' "$rule" 2>/dev/null | head -20)
      local PATHS_LINE
      PATHS_LINE=$(echo "$FM" | grep "^paths:" 2>/dev/null || echo "")

      if [ -n "$PATHS_LINE" ]; then
        # Extract glob patterns from the paths: array (YAML list items)
        local DEAD_GLOBS=""
        local GLOB_COUNT=0
        local LIVE_COUNT=0
        while IFS= read -r line; do
          # Match lines like '  - "**/*.ts"' or '  - **/*.ts'
          local PATTERN
          PATTERN=$(echo "$line" | sed -n 's/^[[:space:]]*-[[:space:]]*"\?\(.*\)"\?$/\1/p' | tr -d '"')
          [ -z "$PATTERN" ] && continue
          GLOB_COUNT=$((GLOB_COUNT + 1))

          # Check if the glob matches at least one file in any project
          local MATCHES
          MATCHES=$(find "$HOME/projects/workspace" -maxdepth 4 -path "$HOME/projects/workspace/$PATTERN" -print -quit 2>/dev/null || echo "")
          if [ -z "$MATCHES" ]; then
            # Try with a broader find using -name for the extension part
            local EXT
            EXT=$(echo "$PATTERN" | grep -oP '\.\{[^}]+\}$' 2>/dev/null || echo "$PATTERN" | grep -oP '\.[a-z]+$' 2>/dev/null || echo "")
            if [ -n "$EXT" ]; then
              # For patterns like **/*.{ts,tsx}, check if any such files exist
              MATCHES=$(find "$HOME/projects/workspace" -maxdepth 5 -type f -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" -o -name "*.py" -o -name "*.gs" -o -name "*.cpp" -o -name "*.hpp" 2>/dev/null | head -1)
            fi
          fi

          if [ -z "$MATCHES" ]; then
            DEAD_GLOBS="${DEAD_GLOBS}${PATTERN}, "
          else
            LIVE_COUNT=$((LIVE_COUNT + 1))
          fi
        done <<< "$(echo "$FM" | grep -A 10 "^paths:" | grep "^[[:space:]]*-")"

        if [ "$GLOB_COUNT" -gt 0 ]; then
          if [ "$LIVE_COUNT" -eq "$GLOB_COUNT" ]; then
            pass "$NAME: paths: $GLOB_COUNT globs, all match files"
          elif [ "$LIVE_COUNT" -gt 0 ]; then
            pass "$NAME: paths: $LIVE_COUNT/$GLOB_COUNT globs match files"
          else
            warn "$NAME: paths: 0/$GLOB_COUNT globs match — rule may be dead"
            RULE_ISSUES=$((RULE_ISSUES + 1))
          fi
        fi
      else
        pass "$NAME: no paths: filter (applies globally)"
      fi
    else
      pass "$NAME: no frontmatter (applies globally)"
    fi
  done

  if [ "$RULE_COUNT" -eq 0 ]; then
    warn "No rule files found"
    SUMMARY_RULES="⚠️  No rules"
  elif [ "$RULE_ISSUES" -gt 0 ]; then
    SUMMARY_RULES="⚠️  rules ($RULE_COUNT) | $RULE_ISSUES with dead path globs"
  else
    SUMMARY_RULES="✅ rules ($RULE_COUNT) | All valid"
  fi
}

# ── Run checks ──
echo "[HEALTH] ~/.claude/ Infrastructure Report"
echo "────────────────────────────────────────────"

case "$COMPONENT" in
  settings) check_settings ;;
  hooks)    check_hooks ;;
  agents)   check_agents ;;
  comms)    check_comms ;;
  sessions) check_sessions ;;
  memory)   check_memory ;;
  rules)  check_rules ;;
  "")
    check_settings
    check_hooks
    check_agents
    check_comms
    check_sessions
    check_memory
    check_rules
    ;;
  *)
    echo "Unknown component: $COMPONENT"
    echo "Valid: settings, hooks, agents, comms, sessions, memory, rules"
    exit 1
    ;;
esac

# ── Summary ──
if [ -z "$COMPONENT" ]; then
  echo ""
  echo "────────────────────────────────────────────"
  echo "Summary:"
  [ -n "$SUMMARY_SETTINGS" ] && echo "  $SUMMARY_SETTINGS"
  [ -n "$SUMMARY_HOOKS" ] && echo "  $SUMMARY_HOOKS"
  [ -n "$SUMMARY_AGENTS" ] && echo "  $SUMMARY_AGENTS"
  [ -n "$SUMMARY_COMMS" ] && echo "  $SUMMARY_COMMS"
  [ -n "$SUMMARY_SESSIONS" ] && echo "  $SUMMARY_SESSIONS"
  [ -n "$SUMMARY_MEMORY" ] && echo "  $SUMMARY_MEMORY"
  [ -n "$SUMMARY_RULES" ] && echo "  $SUMMARY_RULES"
  echo ""
  if [ "$HAS_FAILURE" = true ]; then
    echo "Overall: ❌ HAS FAILURES"
  elif [ "$HAS_WARNING" = true ]; then
    echo "Overall: ⚠️  HEALTHY (with warnings)"
  else
    echo "Overall: ✅ HEALTHY"
  fi
fi

# Exit code
if [ "$HAS_FAILURE" = true ]; then
  exit 1
fi
exit 0
