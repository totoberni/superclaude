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

  # A2: All agents have valid model values
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
        opus|sonnet|haiku) ;;  # valid
        *) ALL_MODELS=false; fail "A2 model values" "$NAME: unknown model '$MODEL_VAL'" ;;
      esac
    fi
  done
  [ "$ALL_MODELS" = true ] && pass "A2 all agents have valid model (opus/sonnet/haiku)"

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
    # Skip special files
    [ "$NAME" = "_archive" ] && continue
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
    # Check that DIR-NNN entries exist and use consistent numbering
    local DIR_ENTRIES
    DIR_ENTRIES=$(grep -oP 'DIR-\d+' "$DIRECTIVES_FILE" 2>/dev/null | sort -u)
    if [ -z "$DIR_ENTRIES" ]; then
      # Some comms dirs may have placeholder directives — just warn
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

  # M2: Inline load-order paths in agent definitions exist
  local ALL_LOAD=true
  for agent in "$AGENT_DIR"/*.md; do
    [ -f "$agent" ] || continue
    [ -L "$agent" ] && continue
    local NAME
    NAME=$(basename "$agent" .md)
    # Only check agents with Memory Load Order sections
    if grep -q "## Memory Load Order" "$agent" 2>/dev/null; then
      # Extract agent-memory paths from the load order section
      local PATHS
      PATHS=$(sed -n '/## Memory Load Order/,/^##/p' "$agent" 2>/dev/null | grep -oP '`agent-memory/[^`]+`' | tr -d '`')
      while IFS= read -r relpath; do
        [ -z "$relpath" ] && continue
        # These are pattern paths (contain <...>), skip them
        echo "$relpath" | grep -q '<' && continue
        local FULLPATH="$HOME/.claude/$relpath"
        if [ ! -e "$FULLPATH" ]; then
          ALL_LOAD=false
          fail "M2 load-order paths" "$NAME: missing $relpath"
        fi
      done <<< "$PATHS"
    fi
  done
  [ "$ALL_LOAD" = true ] && pass "M2 inline load-order paths in agents resolve"

  # M3: No orphan matrix directories (dir exists but no .md inside)
  local ALL_POPULATED=true
  # Row 2: class dirs must have mtm.md
  for cdir in "$MEM_DIR"/class/*/; do
    [ -d "$cdir" ] || continue
    local CNAME
    CNAME=$(basename "$cdir")
    [ "$CNAME" = "projects" ] && continue  # v3 tier
    [ "$CNAME" = "archive" ] && continue
    if [ ! -f "$cdir/mtm.md" ]; then
      ALL_POPULATED=false
      fail "M3 orphan dirs" "class/$CNAME: no mtm.md"
    fi
  done
  # Row 3: instance dirs must have MEMORY.md
  for idir in "$MEM_DIR"/instance/*/; do
    [ -d "$idir" ] || continue
    local INAME
    INAME=$(basename "$idir")
    if [ ! -f "$idir/MEMORY.md" ]; then
      ALL_POPULATED=false
      fail "M3 orphan dirs" "instance/$INAME: no MEMORY.md"
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
    *)
      echo "Unknown component: $COMPONENT"
      echo "Valid: hooks, settings, agents, skills, rules, comms, matrix"
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
