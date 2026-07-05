#!/bin/bash
# Infrastructure regression test suite for ~/.claude/
# Validates agents, skills, rules, comms, settings, and hooks.
#
# Usage:
#   bash ~/.claude/scripts/infra-test.sh            # Full suite (all categories)
#   bash ~/.claude/scripts/infra-test.sh --quick     # Hooks + settings only (~5s)
#   bash ~/.claude/scripts/infra-test.sh --component agents   # Single category
#   bash ~/.claude/scripts/infra-test.sh --no-color  # CI-friendly
#
# Exit codes: 0 = all pass, 1 = any failure, 2 = script error
# Tests are READ-ONLY — never modifies infrastructure.

set -uo pipefail

# ── Args ──
QUICK=false
USE_COLOR=true
COMPONENT=""
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=true ;;
    --no-color) USE_COLOR=false ;;
    --full) ;;  # default
    --component) COMPONENT="__NEXT__" ;;
    *)
      if [ "$COMPONENT" = "__NEXT__" ]; then
        COMPONENT="$arg"
      fi
      ;;
  esac
done
[ "$COMPONENT" = "__NEXT__" ] && COMPONENT=""

# ── Color output ──
if [ "$USE_COLOR" = true ] && [ -t 1 ]; then
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[1;33m'
  CYAN='\033[0;36m'
  NC='\033[0m'
else
  GREEN='' RED='' YELLOW='' CYAN='' NC=''
fi

# ── Counters ──
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
TOTAL_COUNT=0

pass() {
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  PASS_COUNT=$((PASS_COUNT + 1))
  printf "  ${GREEN}PASS${NC}: %s\n" "$1"
}

fail() {
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf "  ${RED}FAIL${NC}: %s -- %s\n" "$1" "$2"
}

warn() {
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  WARN_COUNT=$((WARN_COUNT + 1))
  printf "  ${YELLOW}WARN${NC}: %s -- %s\n" "$1" "$2"
}

section() {
  printf "\n${CYAN}── %s ──${NC}\n" "$1"
}

# ── Paths ──
CLAUDE_DIR="$HOME/.claude"
SETTINGS="$CLAUDE_DIR/settings.json"
AGENT_DIR="$CLAUDE_DIR/agents"
SKILL_DIR="$CLAUDE_DIR/skills"
RULE_DIR="$CLAUDE_DIR/rules"
COMMS_DIR="$CLAUDE_DIR/comms"
HOOK_DIR="$CLAUDE_DIR/hooks"
SCRIPT_DIR="$CLAUDE_DIR/scripts"

echo "=== Infrastructure Regression Suite ($(date '+%Y-%m-%d %H:%M:%S')) ==="
echo "Mode: $([ "$QUICK" = true ] && echo "quick" || echo "${COMPONENT:-full}")"

# ═══════════════════════════════════════════════════
# ST: Settings Tests
# ═══════════════════════════════════════════════════
test_settings() {
  section "Settings Tests (ST)"

  # ST1: Valid JSON
  if jq . "$SETTINGS" >/dev/null 2>&1; then
    pass "ST1 settings.json valid JSON"
  else
    fail "ST1 settings.json" "jq parse failed"
    return  # nothing else meaningful if JSON is broken
  fi

  # ST2: Hook scripts referenced in settings exist on disk
  local ALL_EXIST=true
  local HOOK_CMDS
  HOOK_CMDS=$(jq -r '.. | .command? // empty' "$SETTINGS" 2>/dev/null | sort -u)
  while IFS= read -r cmd; do
    [ -z "$cmd" ] && continue
    # Extract script path — handle $HOME and quotes
    local SCRIPT_PATH
    SCRIPT_PATH=$(echo "$cmd" | grep -oP '\$HOME/\.claude/hooks/\S+\.sh' 2>/dev/null | head -1)
    if [ -n "$SCRIPT_PATH" ]; then
      local EXPANDED="${SCRIPT_PATH/\$HOME/$HOME}"
      if [ ! -f "$EXPANDED" ]; then
        ALL_EXIST=false
        fail "ST2 hook scripts exist" "missing: $EXPANDED"
      fi
    fi
  done <<< "$HOOK_CMDS"
  [ "$ALL_EXIST" = true ] && pass "ST2 hook scripts referenced in settings all exist"

  # ST3: Deny floor >= 5
  local DENY_COUNT
  DENY_COUNT=$(jq '.permissions.deny | length' "$SETTINGS" 2>/dev/null || echo "0")
  if [[ "$DENY_COUNT" =~ ^[0-9]+$ ]] && [ "$DENY_COUNT" -ge 5 ]; then
    pass "ST3 deny floor ($DENY_COUNT rules, >= 5)"
  else
    fail "ST3 deny floor" "only $DENY_COUNT deny rules (need >= 5)"
  fi
}

# ═══════════════════════════════════════════════════
# H: Hook Tests (delegates to test-hooks.sh)
# ═══════════════════════════════════════════════════
test_hooks() {
  section "Hook Tests (H)"

  local TEST_HOOKS="$SCRIPT_DIR/test-hooks.sh"
  if [ ! -f "$TEST_HOOKS" ]; then
    fail "H0 test-hooks.sh" "script not found"
    return
  fi

  # H0: All modules pass bash -n
  local ALL_SYNTAX=true
  for mod in "$HOOK_DIR"/modules/[0-9]*.sh; do
    [ -f "$mod" ] || continue
    if ! bash -n "$mod" 2>/dev/null; then
      ALL_SYNTAX=false
      fail "H0 module syntax" "$(basename "$mod") fails bash -n"
    fi
  done
  [ "$ALL_SYNTAX" = true ] && pass "H0 module syntax (all pass bash -n)"

  # H1: Delegate to test-hooks.sh (quick or full depending on mode)
  local HOOK_ARGS=""
  [ "$QUICK" = true ] && HOOK_ARGS="--quick"
  [ "$USE_COLOR" = false ] && HOOK_ARGS="$HOOK_ARGS --no-color"
  local HOOK_OUTPUT
  HOOK_OUTPUT=$(bash "$TEST_HOOKS" $HOOK_ARGS 2>&1)
  local HOOK_RC=$?
  # Extract summary line from test-hooks.sh output
  local HOOK_SUMMARY
  HOOK_SUMMARY=$(echo "$HOOK_OUTPUT" | grep -E 'Tests:.*Pass:.*Fail:' | tail -1)
  if [ $HOOK_RC -eq 0 ]; then
    pass "H1 hook test suite ($HOOK_SUMMARY)"
  else
    fail "H1 hook test suite" "exit $HOOK_RC ($HOOK_SUMMARY)"
    # Show failures for debugging
    echo "$HOOK_OUTPUT" | grep -E 'FAIL' | while IFS= read -r line; do
      printf "     %s\n" "$line"
    done
  fi
}

# ═══════════════════════════════════════════════════
# A: Agent Tests
# ═══════════════════════════════════════════════════
test_agents() {
  section "Agent Tests (A)"

  local AGENT_COUNT=0
  local REAL_AGENTS=""  # non-symlink, non-archive agents

  for agent in "$AGENT_DIR"/*.md; do
    [ -f "$agent" ] || continue
    local NAME
    NAME=$(basename "$agent" .md)
    # Skip archive dir listing artifacts
    [ "$NAME" = "_archive" ] && continue
    AGENT_COUNT=$((AGENT_COUNT + 1))
    # Track real (non-symlink) agents
    if [ ! -L "$agent" ]; then
      REAL_AGENTS="$REAL_AGENTS $NAME"
    fi
  done

  # A1: All agents have YAML frontmatter
  local ALL_FM=true
  for agent in "$AGENT_DIR"/*.md; do
    [ -f "$agent" ] || continue
    [ -L "$agent" ] && continue  # skip symlinks (tested in A3)
    local NAME
    NAME=$(basename "$agent" .md)
    local FIRST_LINE
    FIRST_LINE=$(head -1 "$agent" 2>/dev/null || echo "")
    if [ "$FIRST_LINE" != "---" ]; then
      ALL_FM=false
      fail "A1 frontmatter" "$NAME: no YAML frontmatter"
    fi
  done
  [ "$ALL_FM" = true ] && pass "A1 all agents have YAML frontmatter"

  # A2: All agents have valid model values (bare or qualified with [1m]/[200k])
  local ALL_MODELS=true
  for agent in "$AGENT_DIR"/*.md; do
    [ -f "$agent" ] || continue
    [ -L "$agent" ] && continue
    local NAME
    NAME=$(basename "$agent" .md)
    local FM
    FM=$(sed -n '2,/^---$/p' "$agent" 2>/dev/null | head -20)
    local MODEL_VAL
    MODEL_VAL=$(echo "$FM" | grep "^model:" | head -1 | awk '{print $2}' | tr -d '"' | tr -d "'")
    if [ -z "$MODEL_VAL" ]; then
      ALL_MODELS=false
      fail "A2 model values" "$NAME: missing model field"
    else
      case "$MODEL_VAL" in
        opus|sonnet|haiku|opus\[1m\]|sonnet\[1m\]|opus\[200k\]|sonnet\[200k\]) ;;  # valid
        *) ALL_MODELS=false; fail "A2 model values" "$NAME: unknown model '$MODEL_VAL'" ;;
      esac
    fi
  done
  [ "$ALL_MODELS" = true ] && pass "A2 all agents have valid model (opus/sonnet/haiku, optionally [1m]/[200k])"

  # A2b: Tiered model policy (main=opus[1m], w-* per DEC-002 matrix)
  # Source: rules/13-worker-first-mandate.md § Per-Worker Defaults
  # Naming convention: w-* → subagent (per-matrix model); everything else → main agent (opus[1m]).
  # Workers not in the map are skipped (allows new w-* additions before policy update).
  declare -A EXPECTED_MODEL=(
    [w-explorer]=haiku
    [w-committer]=haiku
    [w-planner]=opus
    [w-debugger]=sonnet
    [w-design-reviewer]=sonnet
    [w-doc]=sonnet
    [w-implementer]=sonnet
    [w-merger]=sonnet
    [w-refactorer]=sonnet
    [w-reviewer]=sonnet
    [w-tester]=sonnet
  )
  local ALL_TIER=true
  for agent in "$AGENT_DIR"/*.md; do
    [ -f "$agent" ] || continue
    [ -L "$agent" ] && continue
    local NAME
    NAME=$(basename "$agent" .md)
    local FM
    FM=$(sed -n '2,/^---$/p' "$agent" 2>/dev/null | head -20)
    local MODEL_VAL
    MODEL_VAL=$(echo "$FM" | grep "^model:" | head -1 | awk '{print $2}' | tr -d '"' | tr -d "'")
    [ -z "$MODEL_VAL" ] && continue  # A2 already flagged missing field
    case "$NAME" in
      w-*)
        local EXPECTED="${EXPECTED_MODEL[$NAME]:-}"
        [ -z "$EXPECTED" ] && continue  # not in policy map yet — skip
        if [ "$MODEL_VAL" != "$EXPECTED" ]; then
          ALL_TIER=false
          fail "A2b tier policy" "$NAME: should be '$EXPECTED' per matrix (found '$MODEL_VAL')"
        fi
        ;;
      *)
        if [ "$MODEL_VAL" != "opus[1m]" ]; then
          ALL_TIER=false
          fail "A2b tier policy" "$NAME: main agent should be 'opus[1m]' (found '$MODEL_VAL')"
        fi
        ;;
    esac
  done
  [ "$ALL_TIER" = true ] && pass "A2b tier policy (main=opus[1m], w-* per DEC-002 matrix)"

  # A3: Backward-compat symlinks resolve to real files
  local ALL_LINKS=true
  local LINK_COUNT=0
  for agent in "$AGENT_DIR"/*.md; do
    [ -L "$agent" ] || continue
    LINK_COUNT=$((LINK_COUNT + 1))
    local NAME
    NAME=$(basename "$agent" .md)
    if [ ! -e "$agent" ]; then
      ALL_LINKS=false
      fail "A3 symlinks" "$NAME: broken symlink -> $(readlink "$agent" 2>/dev/null)"
    fi
  done
  if [ "$LINK_COUNT" -gt 0 ]; then
    [ "$ALL_LINKS" = true ] && pass "A3 symlinks ($LINK_COUNT links, all resolve)"
  else
    pass "A3 symlinks (none, OK)"
  fi

  # A4: Skill references in agents exist as skills/
  local ALL_SKILLS_REF=true
  for agent in "$AGENT_DIR"/*.md; do
    [ -f "$agent" ] || continue
    [ -L "$agent" ] && continue
    local NAME
    NAME=$(basename "$agent" .md)
    local FM
    # Extract frontmatter body (between first and second ---), excluding delimiters
    FM=$(sed -n '2,/^---$/{ /^---$/d; p; }' "$agent" 2>/dev/null | head -30)
    # Extract skills: list items
    local IN_SKILLS=false
    while IFS= read -r line; do
      if echo "$line" | grep -q "^skills:"; then
        IN_SKILLS=true
        continue
      fi
      if [ "$IN_SKILLS" = true ]; then
        # Check for list item (but not --- delimiter)
        if echo "$line" | grep -q "^---"; then
          break
        fi
        local SKILL_NAME
        SKILL_NAME=$(echo "$line" | sed -n 's/^[[:space:]]*-[[:space:]]*\(.*\)/\1/p' | tr -d '"' | tr -d "'" | xargs 2>/dev/null)
        if [ -n "$SKILL_NAME" ]; then
          if [ ! -d "$SKILL_DIR/$SKILL_NAME" ] || [ ! -f "$SKILL_DIR/$SKILL_NAME/SKILL.md" ]; then
            ALL_SKILLS_REF=false
            fail "A4 skill refs" "$NAME references skill '$SKILL_NAME' which doesn't exist"
          fi
        elif ! echo "$line" | grep -qE '^[[:space:]]*-'; then
          # No longer in skills list
          break
        fi
      fi
    done <<< "$FM"
  done
  [ "$ALL_SKILLS_REF" = true ] && pass "A4 skill references in agents all resolve"

  # A5: Orch-type agents under 300 lines (complexity budget)
  local ALL_SIZE=true
  for agent in "$AGENT_DIR"/*.md; do
    [ -f "$agent" ] || continue
    [ -L "$agent" ] && continue
    local NAME
    NAME=$(basename "$agent" .md)
    # Only check orch-type agents (orch.md, orch-*.md, o-*.md)
    case "$NAME" in
      orch|orch-*|o-*) ;;
      *) continue ;;
    esac
    local LINES
    LINES=$(wc -l < "$agent" 2>/dev/null || echo "0")
    if [[ "$LINES" =~ ^[0-9]+$ ]] && [ "$LINES" -gt 300 ]; then
      ALL_SIZE=false
      warn "A5 orch size" "$NAME: $LINES lines (> 300 budget)"
    fi
  done
  [ "$ALL_SIZE" = true ] && pass "A5 orch agents under 300 lines"
}

# ═══════════════════════════════════════════════════
# S: Skill Tests
# ═══════════════════════════════════════════════════
test_skills() {
  section "Skill Tests (S)"

  # S1: Every skills/ dir has a SKILL.md
  local ALL_EXIST=true
  local SKILL_COUNT=0
  for skill_dir in "$SKILL_DIR"/*/; do
    [ -d "$skill_dir" ] || continue
    SKILL_COUNT=$((SKILL_COUNT + 1))
    local NAME
    NAME=$(basename "$skill_dir")
    if [ ! -f "$skill_dir/SKILL.md" ]; then
      ALL_EXIST=false
      fail "S1 SKILL.md exists" "$NAME: missing SKILL.md"
    fi
  done
  [ "$ALL_EXIST" = true ] && pass "S1 all skill dirs have SKILL.md ($SKILL_COUNT skills)"

  # S2: All SKILL.md have valid frontmatter (--- delimited)
  local ALL_FM=true
  for skill_dir in "$SKILL_DIR"/*/; do
    [ -d "$skill_dir" ] || continue
    local NAME
    NAME=$(basename "$skill_dir")
    local SKILL_FILE="$skill_dir/SKILL.md"
    [ -f "$SKILL_FILE" ] || continue
    local FIRST_LINE
    FIRST_LINE=$(head -1 "$SKILL_FILE" 2>/dev/null || echo "")
    if [ "$FIRST_LINE" != "---" ]; then
      ALL_FM=false
      fail "S2 skill frontmatter" "$NAME: no YAML frontmatter"
    fi
  done
  [ "$ALL_FM" = true ] && pass "S2 all skills have YAML frontmatter"

  # S3: Tool names in frontmatter are valid
  local VALID_TOOLS="Read Write Edit Bash Glob Grep Agent"
  local ALL_TOOLS=true
  for skill_dir in "$SKILL_DIR"/*/; do
    [ -d "$skill_dir" ] || continue
    local NAME
    NAME=$(basename "$skill_dir")
    local SKILL_FILE="$skill_dir/SKILL.md"
    [ -f "$SKILL_FILE" ] || continue
    local FM
    FM=$(sed -n '2,/^---$/p' "$SKILL_FILE" 2>/dev/null | head -20)
    local TOOLS_LINE
    TOOLS_LINE=$(echo "$FM" | grep "^tools:" | head -1)
    [ -z "$TOOLS_LINE" ] && continue  # tools: is optional
    # Extract comma-separated tool names
    local TOOLS_CSV
    TOOLS_CSV=$(echo "$TOOLS_LINE" | sed 's/^tools:[[:space:]]*//' | tr ',' '\n' | tr -d ' "'"'"'')
    while IFS= read -r tool; do
      [ -z "$tool" ] && continue
      local FOUND=false
      for valid in $VALID_TOOLS; do
        [ "$tool" = "$valid" ] && FOUND=true && break
      done
      if [ "$FOUND" = false ]; then
        ALL_TOOLS=false
        fail "S3 tool names" "$NAME: invalid tool '$tool'"
      fi
    done <<< "$TOOLS_CSV"
  done
  [ "$ALL_TOOLS" = true ] && pass "S3 all skill tool names are valid"

  # S4: No duplicate skill dirs
  local DUPES
  DUPES=$(ls -1 "$SKILL_DIR" 2>/dev/null | sort | uniq -d)
  if [ -z "$DUPES" ]; then
    pass "S4 no duplicate skill directories"
  else
    fail "S4 duplicate skills" "duplicates: $DUPES"
  fi

  # S5: Agent-only skills referenced by at least one agent
  local ALL_AGENTONLY=true
  for skill_dir in "$SKILL_DIR"/*/; do
    [ -d "$skill_dir" ] || continue
    local NAME
    NAME=$(basename "$skill_dir")
    local SKILL_FILE="$skill_dir/SKILL.md"
    [ -f "$SKILL_FILE" ] || continue
    local FM
    FM=$(sed -n '2,/^---$/p' "$SKILL_FILE" 2>/dev/null | head -20)
    # Check for agent_only or agentOnly flag
    if echo "$FM" | grep -qiE '(agent_only|agentOnly):\s*(true|yes)'; then
      # This is agent-only — verify at least one agent references it
      local REFERENCED=false
      for agent in "$AGENT_DIR"/*.md; do
        [ -f "$agent" ] || continue
        if grep -q "$NAME" "$agent" 2>/dev/null; then
          REFERENCED=true
          break
        fi
      done
      if [ "$REFERENCED" = false ]; then
        ALL_AGENTONLY=false
        warn "S5 agent-only refs" "$NAME: agent-only but no agent references it"
      fi
    fi
  done
  [ "$ALL_AGENTONLY" = true ] && pass "S5 agent-only skills all referenced"
}

# ═══════════════════════════════════════════════════
# R: Rule Tests
# ═══════════════════════════════════════════════════
test_rules() {
  section "Rule Tests (R)"

  # R1: All rule files start with # heading (valid markdown)
  local ALL_MD=true
  local RULE_COUNT=0
  for rule in "$RULE_DIR"/*.md; do
    [ -f "$rule" ] || continue
    RULE_COUNT=$((RULE_COUNT + 1))
    local NAME
    NAME=$(basename "$rule")
    local FIRST_CONTENT
    # Skip frontmatter if present, find first # heading
    if [ "$(head -1 "$rule" 2>/dev/null)" = "---" ]; then
      FIRST_CONTENT=$(sed -n '/^---$/,/^---$/!p' "$rule" 2>/dev/null | grep -m1 "^#" || echo "")
    else
      FIRST_CONTENT=$(head -1 "$rule" 2>/dev/null || echo "")
    fi
    if ! echo "$FIRST_CONTENT" | grep -q "^#"; then
      ALL_MD=false
      fail "R1 markdown format" "$NAME: no # heading found"
    fi
  done
  [ "$ALL_MD" = true ] && pass "R1 all rules have # heading ($RULE_COUNT rules)"

  # R2: Sequential numbering (files match NN-*.md pattern)
  local ALL_NUMBERED=true
  local PREV_NUM=-1
  for rule in "$RULE_DIR"/*.md; do
    [ -f "$rule" ] || continue
    local NAME
    NAME=$(basename "$rule")
    local NUM
    NUM=$(echo "$NAME" | grep -oP '^\d+' 2>/dev/null || echo "")
    if [ -z "$NUM" ]; then
      ALL_NUMBERED=false
      fail "R2 numbering" "$NAME: doesn't start with NN-"
    else
      # Check for reasonable gaps (not duplicates)
      local NUM_INT
      NUM_INT=$((10#$NUM))
      if [ "$NUM_INT" -eq "$PREV_NUM" ]; then
        ALL_NUMBERED=false
        fail "R2 numbering" "$NAME: duplicate number $NUM"
      fi
      PREV_NUM=$NUM_INT
    fi
  done
  [ "$ALL_NUMBERED" = true ] && pass "R2 rule numbering is sequential (no duplicates)"

  # R3: Rules with paths: globs — check at least one glob matches files
  local ALL_PATHS=true
  for rule in "$RULE_DIR"/*.md; do
    [ -f "$rule" ] || continue
    local NAME
    NAME=$(basename "$rule")
    local FIRST_LINE
    FIRST_LINE=$(head -1 "$rule" 2>/dev/null || echo "")
    [ "$FIRST_LINE" = "---" ] || continue  # no frontmatter = global rule, skip
    local FM
    FM=$(sed -n '2,/^---$/p' "$rule" 2>/dev/null | head -20)
    local PATHS_LINE
    PATHS_LINE=$(echo "$FM" | grep "^paths:" 2>/dev/null || echo "")
    [ -z "$PATHS_LINE" ] && continue  # no paths = global, skip
    # Check that the rule has at least one valid path entry
    local PATH_ITEMS
    PATH_ITEMS=$(echo "$FM" | grep -A 20 "^paths:" | grep "^[[:space:]]*-" | wc -l)
    if [ "$PATH_ITEMS" -eq 0 ]; then
      ALL_PATHS=false
      fail "R3 path globs" "$NAME: paths: declared but no items listed"
    fi
  done
  [ "$ALL_PATHS" = true ] && pass "R3 path globs valid (rules with paths: have items)"
}

# ═══════════════════════════════════════════════════
# C: Comms Tests
# ═══════════════════════════════════════════════════
test_comms() {
  section "Comms Tests (C)"

  local REQUIRED_FILES="bootstrap.md directives.md escalations.md reports.md"
  local COMMS_COUNT=0
  local ALL_COMPLETE=true

  # C1: All comms dirs have 4-file sets
  for dir in "$COMMS_DIR"/*/; do
    [ -d "$dir" ] || continue
    local NAME
    NAME=$(basename "$dir")
    # Skip special dirs (meta has non-standard comms structure — writes TO orch dirs)
    [ "$NAME" = "_archive" ] && continue
    [ "$NAME" = "meta" ] && continue
    COMMS_COUNT=$((COMMS_COUNT + 1))
    local MISSING=""
    for req in $REQUIRED_FILES; do
      [ -f "$dir/$req" ] || MISSING="$MISSING $req"
    done
    if [ -n "$MISSING" ]; then
      ALL_COMPLETE=false
      fail "C1 4-file sets" "$NAME: missing$MISSING"
    fi
  done
  [ "$ALL_COMPLETE" = true ] && pass "C1 all comms dirs complete ($COMMS_COUNT dirs)"

  # C2: Agent cross-ref (comms dir has matching agent, or is a known pattern)
  local ALL_XREF=true
  for dir in "$COMMS_DIR"/*/; do
    [ -d "$dir" ] || continue
    local NAME
    NAME=$(basename "$dir")
    [ "$NAME" = "_archive" ] && continue
    # Check for direct agent match, or known prefixed pattern
    if [ -f "$AGENT_DIR/$NAME.md" ]; then
      continue  # direct match
    fi
    # Named orchs (orch-*, o-*) reference orch.md base
    case "$NAME" in
      orch-*|o-*) [ -f "$AGENT_DIR/orch.md" ] && continue ;;
    esac
    # scaf special case
    case "$NAME" in
      scaf|scaf2) continue ;;  # known infra agents
    esac
    ALL_XREF=false
    warn "C2 agent cross-ref" "$NAME: no matching agent definition"
  done
  [ "$ALL_XREF" = true ] && pass "C2 comms dirs cross-reference valid agents"

  # C3: DIR numbering in directives.md files
  local ALL_NUMS=true
  for dir in "$COMMS_DIR"/*/; do
    [ -d "$dir" ] || continue
    local NAME
    NAME=$(basename "$dir")
    [ "$NAME" = "_archive" ] && continue
    local DIRECTIVES_FILE="$dir/directives.md"
    [ -f "$DIRECTIVES_FILE" ] || continue
    # Skip empty placeholder ledgers: a 0-byte directives.md is an idle infra dir,
    # not an active orch with missing directives — warning on it is a false-positive.
    # Files WITH content but no DIR-NNN still warn below (the real "forgot to number" case).
    [ -s "$DIRECTIVES_FILE" ] || continue
    # Skip decommissioned orchs — their directives are intentionally cleared
    if grep -qi "decommissioned" "$DIRECTIVES_FILE" 2>/dev/null; then
      continue
    fi
    # Check that DIR-NNN entries exist and use consistent numbering
    local DIR_ENTRIES
    DIR_ENTRIES=$(grep -oP 'DIR-\d+' "$DIRECTIVES_FILE" 2>/dev/null | sort -u)
    if [ -z "$DIR_ENTRIES" ]; then
      # Active orchs should have at least one directive
      warn "C3 DIR numbering" "$NAME: no DIR-NNN entries in directives.md"
      continue
    fi
    # Check for split directives: if directives/ subdir exists, verify active entries have files
    if [ -d "$dir/directives" ]; then
      local MISSING_FILES=""
      for dir_entry in $DIR_ENTRIES; do
        # Only check entries in the status table with active states (PENDING/DONE/IN_PROGRESS)
        local STATUS_LINE
        STATUS_LINE=$(grep -E "^\|.*$dir_entry.*\|.*(PENDING|DONE|IN_PROGRESS)" "$DIRECTIVES_FILE" 2>/dev/null | head -1)
        [ -z "$STATUS_LINE" ] && continue
        if [ ! -f "$dir/directives/$dir_entry.md" ]; then
          MISSING_FILES="$MISSING_FILES $dir_entry"
        fi
      done
      if [ -n "$MISSING_FILES" ]; then
        ALL_NUMS=false
        warn "C3 DIR files" "$NAME: missing directive files:$MISSING_FILES"
      fi
    fi
  done
  [ "$ALL_NUMS" = true ] && pass "C3 DIR numbering and files consistent"

  # C4: Comms search-store deep integrity (v3 Phase 2, T2.4)
  # The FTS5+sqlite-vec store at comms/.comms.db must satisfy rows==fts==vec.
  # This is the DEEP check: it counts memories_vec, which needs the sqlite-vec
  # extension and therefore the venv python (plain sqlite3 cannot). super-health's
  # bash facet only checks rows==fts (+table presence) since it has no extension.
  #   ABSENT → SKIP (warn, not fail) — fail-safe for pre-comms-DB systems.
  #   PRESENT → run comms_db.py stats offline, assert rows==fts_rows==vec_rows.
  local COMMS_DB="$COMMS_DIR/.comms.db"
  local CDB_PY="$SCRIPT_DIR/memory/comms_db.py"
  local CDB_VENV="$CLAUDE_DIR/.venv/bin/python"
  if [ ! -f "$COMMS_DB" ]; then
    warn "C4 comms search-store" "comms/.comms.db not built — skipping deep integrity check"
  elif [ ! -x "$CDB_VENV" ] || [ ! -f "$CDB_PY" ]; then
    warn "C4 comms search-store" "venv python or comms_db.py missing — cannot run stats"
  else
    local CDB_OUT CDB_RC CDB_ROWS CDB_FTS CDB_VEC
    CDB_OUT=$(HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 "$CDB_VENV" "$CDB_PY" stats 2>&1)
    CDB_RC=$?
    CDB_ROWS=$(echo "$CDB_OUT" | grep -E '^[[:space:]]*rows[[:space:]]*:' | grep -oE '[0-9]+' | head -1)
    CDB_FTS=$(echo "$CDB_OUT" | grep -E '^[[:space:]]*fts_rows[[:space:]]*:' | grep -oE '[0-9]+' | head -1)
    CDB_VEC=$(echo "$CDB_OUT" | grep -E '^[[:space:]]*vec_rows[[:space:]]*:' | grep -oE '[0-9]+' | head -1)
    if [ "$CDB_RC" -ne 0 ]; then
      fail "C4 comms search-store" "comms_db.py stats exit $CDB_RC"
    elif [ -z "$CDB_ROWS" ] || [ -z "$CDB_FTS" ] || [ -z "$CDB_VEC" ]; then
      fail "C4 comms search-store" "could not parse rows/fts_rows/vec_rows from stats"
    elif [ "$CDB_ROWS" = "$CDB_FTS" ] && [ "$CDB_ROWS" = "$CDB_VEC" ]; then
      pass "C4 comms search-store integrity (rows==fts==vec==$CDB_ROWS)"
    else
      fail "C4 comms search-store" "rows/fts/vec mismatch: rows=$CDB_ROWS fts=$CDB_FTS vec=$CDB_VEC"
    fi
  fi
}

# ═══════════════════════════════════════════════════
# M: Memory Matrix Tests
# ═══════════════════════════════════════════════════
test_matrix() {
  section "Memory Matrix Tests (M)"

  local MEM_DIR="$CLAUDE_DIR/agent-memory"

  # M1: Root symlinks resolve to instance/ dirs
  local ALL_LINKS=true
  local LINK_COUNT=0
  for link in "$MEM_DIR"/*/; do
    [ -L "${link%/}" ] || continue
    local NAME
    NAME=$(basename "$link")
    # Skip structural symlinks
    case "$NAME" in
      _archive|_compact-snapshots) continue ;;
    esac
    LINK_COUNT=$((LINK_COUNT + 1))
    if [ ! -d "$link" ]; then
      ALL_LINKS=false
      fail "M1 root symlinks" "$NAME: broken -> $(readlink "${link%/}" 2>/dev/null)"
    fi
  done
  if [ "$LINK_COUNT" -gt 0 ]; then
    [ "$ALL_LINKS" = true ] && pass "M1 root symlinks resolve ($LINK_COUNT links)"
  else
    pass "M1 root symlinks (none, OK)"
  fi

  # M2: Memory-section paths in agent definitions are live, not dead per-file references.
  # v3 migrated from per-file `agent-memory/*.md` paths (old "Memory Load Order" section) to
  # a DB-backed model ("Memory Access" section, see rules/12). Accept either header via a
  # grouped regex so this check keeps working across both generations of agent definitions.
  local ALL_LOAD=true
  for agent in "$AGENT_DIR"/*.md; do
    [ -f "$agent" ] || continue
    [ -L "$agent" ] && continue
    local NAME
    NAME=$(basename "$agent" .md)
    if grep -qE '^## (Memory Load Order|Memory Access)$' "$agent" 2>/dev/null; then
      # Extract the section body (up to the next section header)
      local SECTION
      SECTION=$(sed -n -E '/^## (Memory Load Order|Memory Access)$/,/^##[^#]/p' "$agent" 2>/dev/null)
      # Dead-path check: legacy per-file `agent-memory/*.md` backtick references no longer
      # exist under the v3 DB model, so any literal (non-placeholder) one is dead weight.
      local DEAD_PATHS
      DEAD_PATHS=$(printf '%s\n' "$SECTION" | grep -oP '`agent-memory/[^`]+\.md`' | tr -d '`')
      while IFS= read -r relpath; do
        [ -z "$relpath" ] && continue
        # Pattern paths (contain <...>) are illustrative, not literal, so skip them
        echo "$relpath" | grep -q '<' && continue
        ALL_LOAD=false
        fail "M2 memory-section paths" "$NAME: dead per-file reference $relpath (v3 uses the DB, not MD files)"
      done <<< "$DEAD_PATHS"
      # Live-pointer check: the section must point at the canonical v3 artifacts instead
      # of a dead file path: rules/12 (the memory-access protocol) or memory_db.py/.memory.db
      # (the DB itself).
      if ! printf '%s\n' "$SECTION" | grep -qE 'rules/12|memory_db\.py|\.memory\.db'; then
        ALL_LOAD=false
        fail "M2 memory-section paths" "$NAME: Memory section doesn't reference rules/12 or memory_db.py"
      fi
    fi
  done
  [ "$ALL_LOAD" = true ] && pass "M2 memory-section paths point to live v3 artifacts (rules/12 / memory_db.py), no dead per-file refs"

  # M3: No orphan matrix directories (dir exists but no .md inside)
  # Skip structural/archive containers recursively — they hold decommissioned cells by design.
  local ALL_POPULATED=true
  # Row 2: class dirs must have mtm.md
  for cdir in "$MEM_DIR"/class/*/; do
    [ -d "$cdir" ] || continue
    local CNAME
    CNAME=$(basename "$cdir")
    case "$CNAME" in
      projects|archive|_archive) continue ;;  # v3 tier + archive containers
    esac
    if [ ! -f "$cdir/mtm.md" ]; then
      ALL_POPULATED=false
      fail "M3 orphan dirs" "class/$CNAME: no mtm.md"
    fi
  done
  # Row 3: instance dirs must be populated. v3 is DB-backed: an instance dir holds individual
  # per-memory <name>.md files (indexed by .memory.db), NOT a pre-v3 MEMORY.md index. So an
  # orphan is a dir with NO .md files at all (matches this block's stated intent above), not one
  # merely lacking the obsolete MEMORY.md filename.
  for idir in "$MEM_DIR"/instance/*/; do
    [ -d "$idir" ] || continue
    local INAME
    INAME=$(basename "$idir")
    case "$INAME" in
      archive|_archive) continue ;;  # archive containers, not orch instances
    esac
    if ! ls "$idir"*.md >/dev/null 2>&1 && ! ls "$idir"*/*.md >/dev/null 2>&1; then
      ALL_POPULATED=false
      fail "M3 orphan dirs" "instance/$INAME: empty (no .md memory files)"
    fi
  done
  [ "$ALL_POPULATED" = true ] && pass "M3 no orphan matrix directories"

  # M4: Template files have standard headers
  local ALL_HEADERS=true
  # Check ltm.md
  if [ -f "$MEM_DIR/shared/global/ltm.md" ]; then
    if ! head -1 "$MEM_DIR/shared/global/ltm.md" 2>/dev/null | grep -q "^#"; then
      ALL_HEADERS=false
      fail "M4 headers" "shared/global/ltm.md: no # heading"
    fi
  fi
  # Check mtm.md files
  for mtm in "$MEM_DIR"/class/*/mtm.md; do
    [ -f "$mtm" ] || continue
    if ! head -1 "$mtm" 2>/dev/null | grep -q "^#"; then
      ALL_HEADERS=false
      fail "M4 headers" "$(echo "$mtm" | sed "s|$MEM_DIR/||"): no # heading"
    fi
  done
  [ "$ALL_HEADERS" = true ] && pass "M4 template files have standard headers"
}

# ═══════════════════════════════════════════════════
# SK: SKM Tests (Session Key Manager -- toto-access ephemeral cert minting)
# ═══════════════════════════════════════════════════
test_skm() {
  section "SKM Tests (SK)"

  local SKM_BIN="$CLAUDE_DIR/bin/skm"
  local SKM_AUTH_BIN="$CLAUDE_DIR/bin/skm-authorize"

  # SK1-SK4 need a configured CA (~/.ssh/skm-ca). On a host where SKM was never set up
  # (CI runner, fresh clone) there is no CA and `skm doctor`/`mint` legitimately die --
  # gate on CA presence so those hosts SKIP (not FAIL), mirroring the ABSENT->SKIP
  # convention used for the comms search-store (C4) above.
  if [ ! -f "$HOME/.ssh/skm-ca" ]; then
    warn "SK1-SK4 skm lifecycle" "SKM not configured on this host (no ~/.ssh/skm-ca) -- skipping"
  else
    # SK1: skm doctor -- CA present, deps available, exit 0, prints SKM_DOCTOR_OK.
    local SK1_OUT SK1_RC
    SK1_OUT=$(timeout 15 bash "$SKM_BIN" doctor 2>&1); SK1_RC=$?
    if [ "$SK1_RC" -eq 0 ] && echo "$SK1_OUT" | grep -q "SKM_DOCTOR_OK"; then
      pass "SK1 skm doctor (exit 0, SKM_DOCTOR_OK)"
    else
      fail "SK1 skm doctor" "rc=$SK1_RC out=$SK1_OUT"
    fi

    # SK2-SK4: local-only mint/sock/revoke lifecycle for a throwaway session.
    # SKM_REGISTER_SUDO=0 skips the toto ssh round-trip entirely (no network/toto call).
    # XDG_RUNTIME_DIR points at a scratch dir as best-effort isolation: skm prefers
    # /run/user/$uid over XDG_RUNTIME_DIR whenever that path is writable, so on most
    # dev machines the throwaway session still lands under the real BASE dir (cleaned
    # up defensively below either way); the override only takes effect where
    # /run/user/$uid is unavailable. A short 60s TTL keeps the throwaway session
    # self-expiring even if cleanup below is somehow skipped.
    local SK_SID="infratest-skm-$$-$(date +%s)"
    local SK_SCRATCH
    SK_SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/infratest-skm.XXXXXX" 2>/dev/null)

    local SK_MINT_RC
    XDG_RUNTIME_DIR="$SK_SCRATCH" SKM_REGISTER_SUDO=0 timeout 15 bash "$SKM_BIN" mint "$SK_SID" infratest 60 >/dev/null 2>&1
    SK_MINT_RC=$?

    # SK2: skm sock -- returns a live unix socket path for the just-minted session.
    local SK_SOCK
    SK_SOCK=$(XDG_RUNTIME_DIR="$SK_SCRATCH" timeout 10 bash "$SKM_BIN" sock 2>/dev/null)
    if [ "$SK_MINT_RC" -eq 0 ] && [ -n "$SK_SOCK" ] && [ -S "$SK_SOCK" ]; then
      pass "SK2 skm mint + sock (throwaway session socket resolves)"
    else
      fail "SK2 skm mint + sock" "mint_rc=$SK_MINT_RC sock=$SK_SOCK"
    fi

    # SK3: the session agent holds the minted key (ssh-add -l exits 0 = 1+ identities).
    local SK3_RC=2
    if [ -n "$SK_SOCK" ] && [ -S "$SK_SOCK" ]; then
      SSH_AUTH_SOCK="$SK_SOCK" ssh-add -l >/dev/null 2>&1
      SK3_RC=$?
    fi
    if [ "$SK3_RC" -eq 0 ]; then
      pass "SK3 skm session agent holds a key (ssh-add -l exit 0)"
    else
      fail "SK3 skm session agent" "ssh-add -l rc=$SK3_RC on socket=$SK_SOCK"
    fi

    # SK4: skm revoke -- kills the agent, shreds keys; socket must no longer exist.
    XDG_RUNTIME_DIR="$SK_SCRATCH" timeout 15 bash "$SKM_BIN" revoke "$SK_SID" >/dev/null 2>&1
    local SK4_RC=$?
    local SK4_SOCK_GONE=true
    [ -n "$SK_SOCK" ] && [ -S "$SK_SOCK" ] && SK4_SOCK_GONE=false
    if [ "$SK4_RC" -eq 0 ] && [ "$SK4_SOCK_GONE" = true ]; then
      pass "SK4 skm revoke (session agent + socket removed)"
    else
      fail "SK4 skm revoke" "rc=$SK4_RC socket_gone=$SK4_SOCK_GONE"
    fi

    # Cleanup: throwaway scratch dir + defensive removal from the real BASE (see note
    # above on /run/user/$uid precedence).
    rm -rf "$SK_SCRATCH" 2>/dev/null
    rm -rf "/run/user/$(id -u)/skm/$SK_SID" 2>/dev/null
  fi

  # SK5: skm-authorize -- bash -n clean. Root-only toto-side helper; never executed here.
  # Always runs regardless of CA presence (pure syntax check, no toto/CA dependency).
  if bash -n "$SKM_AUTH_BIN" 2>/dev/null; then
    pass "SK5 skm-authorize bash -n clean"
  else
    fail "SK5 skm-authorize" "bash -n failed"
  fi
}

# ═══════════════════════════════════════════════════
# Run selected tests
# ═══════════════════════════════════════════════════

START_TIME=$(date +%s)

if [ "$QUICK" = true ]; then
  test_hooks
  test_settings
elif [ -n "$COMPONENT" ]; then
  case "$COMPONENT" in
    hooks|hook|H)       test_hooks ;;
    settings|ST)        test_settings ;;
    agents|agent|A)     test_agents ;;
    skills|skill|S)     test_skills ;;
    rules|rule|R)       test_rules ;;
    comms|C)            test_comms ;;
    matrix|memory|M)    test_matrix ;;
    skm|SK)             test_skm ;;
    *)
      echo "Unknown component: $COMPONENT"
      echo "Valid: hooks, settings, agents, skills, rules, comms, matrix, skm"
      exit 2
      ;;
  esac
else
  # Full suite
  test_settings
  test_hooks
  test_agents
  test_skills
  test_rules
  test_comms
  test_matrix
  test_skm
fi

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

# ═══════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════
echo ""
echo "======================================================="
printf "Tests: %d | ${GREEN}Pass: %d${NC} | ${RED}Fail: %d${NC} | ${YELLOW}Warn: %d${NC} | Time: %ds\n" \
  "$TOTAL_COUNT" "$PASS_COUNT" "$FAIL_COUNT" "$WARN_COUNT" "$DURATION"
echo "======================================================="

[ "$FAIL_COUNT" -gt 0 ] && exit 1
exit 0
