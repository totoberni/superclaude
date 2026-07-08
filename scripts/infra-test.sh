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

  # A6: wf-skills delegation grants (W1.1). meta.md must grant SendMessage, Skill,
  # WebSearch, WebFetch on its tools line; orch.md must grant SendMessage, Skill.
  # Real failing path: a frontmatter rewrite that drops any grant fails here.
  local GRANTS_OK=true META_TOOLS ORCH_TOOLS g
  META_TOOLS=$(grep -m1 -E '^tools:' "$AGENT_DIR/meta.md" 2>/dev/null)
  ORCH_TOOLS=$(grep -m1 -E '^tools:' "$AGENT_DIR/orch.md" 2>/dev/null)
  for g in SendMessage Skill WebSearch WebFetch; do
    echo "$META_TOOLS" | grep -qw "$g" || { GRANTS_OK=false; fail "A6 grants" "meta.md tools missing $g"; }
  done
  for g in SendMessage Skill; do
    echo "$ORCH_TOOLS" | grep -qw "$g" || { GRANTS_OK=false; fail "A6 grants" "orch.md tools missing $g"; }
  done
  [ "$GRANTS_OK" = true ] && pass "A6 wf-skills grants (meta: SendMessage/Skill/WebSearch/WebFetch; orch: SendMessage/Skill)"

  # A7: wf-skills fleet contract (W1.7). Each w-*.md must carry EXACTLY ONE
  # "## Report Contract (wf-skills)" section; the Skill tool must be on the tools
  # line of the 9 reasoning workers and ABSENT from w-committer and w-explorer
  # (read-only / low-reasoning classes must not gain Skill). Real failing paths:
  # a dropped/duplicated contract section, a missing Skill grant on a reasoning
  # worker, or Skill leaking onto a read-only class.
  local FLEET_OK=true W_COUNT=0 wf wname wcc wtl
  local REASONING=" w-reviewer w-design-reviewer w-doc w-implementer w-debugger w-tester w-merger w-refactorer w-planner "
  local NOSKILL=" w-committer w-explorer "
  for wf in "$AGENT_DIR"/w-*.md; do
    [ -f "$wf" ] || continue
    [ -L "$wf" ] && continue
    W_COUNT=$((W_COUNT + 1))
    wname=$(basename "$wf" .md)
    wcc=$(grep -c '^## Report Contract (wf-skills)$' "$wf" 2>/dev/null)
    if [ "$wcc" != "1" ]; then
      FLEET_OK=false
      fail "A7 fleet contract" "$wname: expected exactly 1 report-contract section, found $wcc"
    fi
    wtl=$(grep -m1 -E '^tools:' "$wf" 2>/dev/null)
    case "$REASONING" in
      *" $wname "*)
        echo "$wtl" | grep -qw Skill || { FLEET_OK=false; fail "A7 fleet Skill grant" "$wname (reasoning worker): Skill missing from tools"; } ;;
    esac
    case "$NOSKILL" in
      *" $wname "*)
        echo "$wtl" | grep -qw Skill && { FLEET_OK=false; fail "A7 fleet Skill grant" "$wname (read-only class): Skill must be ABSENT from tools"; } ;;
    esac
  done
  if [ "$W_COUNT" -lt 11 ]; then
    FLEET_OK=false
    fail "A7 fleet contract" "expected >= 11 w-*.md agents, found $W_COUNT"
  fi
  [ "$FLEET_OK" = true ] && pass "A7 wf-skills fleet ($W_COUNT workers: 1 report-contract each; Skill on 9 reasoning, absent from w-committer/w-explorer)"

  # A8: read-only class write-lock (W0.8). The tools: allowlist alone does
  # not reliably exclude Write/Edit (w-hostile-reviewer showed Write+Edit
  # despite a read-only tools: declaration). Each of the 6 read-only classes
  # must carry a disallowedTools: line excluding both Write and Edit. Guard:
  # none of the 6 write-capable workers may have Write blocked, so this
  # check cannot be trivially satisfied by blanket-adding disallowedTools
  # everywhere. Real failing paths: a read-only class missing the
  # exclusion, or a write-capable worker wrongly blocked from Write.
  local RO_LOCK_OK=true roname rof rodt
  for roname in w-reviewer w-design-reviewer w-explorer w-tester w-committer w-hostile-reviewer; do
    rof="$AGENT_DIR/$roname.md"
    [ -f "$rof" ] || { RO_LOCK_OK=false; fail "A8 read-only write-lock" "$roname: agent file not found"; continue; }
    rodt=$(grep -m1 -E '^disallowedTools:' "$rof" 2>/dev/null)
    echo "$rodt" | grep -qw Write || { RO_LOCK_OK=false; fail "A8 read-only write-lock" "$roname: disallowedTools missing Write exclusion"; }
    echo "$rodt" | grep -qw Edit || { RO_LOCK_OK=false; fail "A8 read-only write-lock" "$roname: disallowedTools missing Edit exclusion"; }
  done
  for roname in w-implementer w-doc w-refactorer w-merger w-debugger w-planner; do
    rof="$AGENT_DIR/$roname.md"
    [ -f "$rof" ] || continue
    rodt=$(grep -m1 -E '^disallowedTools:' "$rof" 2>/dev/null)
    echo "$rodt" | grep -qw Write && { RO_LOCK_OK=false; fail "A8 write-capable guard" "$roname: Write wrongly blocked by disallowedTools"; }
  done
  [ "$RO_LOCK_OK" = true ] && pass "A8 read-only write-lock (6 classes disallowedTools excludes Write+Edit; 6 write-capable workers unblocked)"
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
    local NAME
    NAME=$(basename "$skill_dir")
    # Only _shared and _archive are structural support libraries, NOT skills, and
    # have no SKILL.md by design (_shared holds the 8 wf-skills rubric blocks;
    # _archive holds retired skills). They are validated by their own dedicated
    # checks (S6 covers _shared). Mirrors the _archive skip already used in
    # test_agents/test_comms. m2 fix (wf-skills review round 1): narrowed from
    # skipping every "_*" dir to exactly these two names, so a future real
    # skill dir that happens to start with "_" is still required to have a
    # SKILL.md below, same as any other skill dir.
    case "$NAME" in _shared|_archive) continue ;; esac
    SKILL_COUNT=$((SKILL_COUNT + 1))
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

  # S6: wf-skills _shared rubric-block integrity (W1.2). The 8 shared blocks must all
  # exist, each must declare a "Consumed by:" provenance line, and none may contain an
  # em-dash (U+2014) or en-dash (U+2013) byte (writing-style rule). Real failing paths:
  # a deleted block, a block missing its Consumed-by pointer, or a stray dash byte.
  # m2 fix (wf-skills review round 1): the Consumed-by + dash-clean checks below now
  # walk every actual _shared/*.md on disk, not just the 8 named blocks, so a 9th or
  # unlisted block cannot escape validation. The 8 required blocks are still asserted
  # present BY NAME first (that check is unchanged).
  local SHARED_DIR="$SKILL_DIR/_shared"
  local SHARED_BLOCKS="verdict-schema dispatch-contract helper-prompt retro-evidence diff-target discovery-protocol search-budget memory-distill"
  local SHARED_OK=true sb sbf
  for sb in $SHARED_BLOCKS; do
    sbf="$SHARED_DIR/$sb.md"
    if [ ! -f "$sbf" ]; then
      SHARED_OK=false
      fail "S6 _shared integrity" "$sb.md: missing"
    fi
  done
  for sbf in "$SHARED_DIR"/*.md; do
    [ -f "$sbf" ] || continue
    sb=$(basename "$sbf" .md)
    grep -q 'Consumed by:' "$sbf" 2>/dev/null || { SHARED_OK=false; fail "S6 _shared integrity" "$sb.md: no 'Consumed by:' line"; }
    if grep -qP '\xe2\x80[\x93\x94]' "$sbf" 2>/dev/null; then
      SHARED_OK=false
      fail "S6 _shared integrity" "$sb.md: contains em-dash/en-dash byte"
    fi
  done
  [ "$SHARED_OK" = true ] && pass "S6 _shared integrity (8 required blocks present; all _shared/*.md Consumed-by present, dash-clean)"

  # S7: wf-skills new loop-driver skills (W1.4/W1.5). converge + review-dispatch must
  # exist with frontmatter carrying name/description/user-invocable, must NOT carry a
  # disable-model-invocation key (DEC-R3 flip), and must be em/en-dash clean. Real
  # failing paths: a missing skill, a dropped required key, a re-added flip key, a dash.
  local NEW_SKILLS="converge review-dispatch"
  local NEWSK_OK=true ns nsf nsfm
  for ns in $NEW_SKILLS; do
    nsf="$SKILL_DIR/$ns/SKILL.md"
    if [ ! -f "$nsf" ]; then
      NEWSK_OK=false
      fail "S7 new skills" "$ns/SKILL.md: missing"
      continue
    fi
    nsfm=$(sed -n '2,/^---$/p' "$nsf" 2>/dev/null)
    echo "$nsfm" | grep -qE '^name:'            || { NEWSK_OK=false; fail "S7 new skills" "$ns: frontmatter missing name:"; }
    echo "$nsfm" | grep -qE '^description:'      || { NEWSK_OK=false; fail "S7 new skills" "$ns: frontmatter missing description:"; }
    echo "$nsfm" | grep -qE '^user-invocable:'   || { NEWSK_OK=false; fail "S7 new skills" "$ns: frontmatter missing user-invocable:"; }
    echo "$nsfm" | grep -qE '^disable-model-invocation:' && { NEWSK_OK=false; fail "S7 new skills" "$ns: disable-model-invocation must be absent (DEC-R3)"; }
    if grep -qP '\xe2\x80[\x93\x94]' "$nsf" 2>/dev/null; then
      NEWSK_OK=false
      fail "S7 new skills" "$ns: contains em-dash/en-dash byte"
    fi
  done
  [ "$NEWSK_OK" = true ] && pass "S7 new skills (converge + review-dispatch: name/description/user-invocable, no flip key, dash-clean)"

  # S8: wf-skills DEC-R3 flip invariant (W3.5). The 2026-07-07 flip deleted
  # disable-model-invocation: true from all skills so every skill is model-
  # invocable + loop-able; the invariant is that the key never reappears with
  # value true on any skills/*/SKILL.md. Read directly off disk (not via git)
  # so gitignored skills/nudge/SKILL.md (.gitignore:25 nudge/) is still covered.
  # Real failing path: any skill (existing or new) re-introducing the key.
  local FLIP_OK=true
  local FLIP_HITS
  FLIP_HITS=$(grep -rl '^disable-model-invocation: true$' "$SKILL_DIR"/*/SKILL.md 2>/dev/null)
  if [ -n "$FLIP_HITS" ]; then
    FLIP_OK=false
    fail "S8 DEC-R3 flip invariant" "disable-model-invocation: true re-introduced in: $(echo "$FLIP_HITS" | tr '\n' ' ')"
  fi
  [ "$FLIP_OK" = true ] && pass "S8 DEC-R3 flip invariant (zero skills carry disable-model-invocation: true, incl. gitignored nudge)"

  # S9: wf-skills destructive-tier unattended-context gate (W3.5). Each of
  # skills/push, skills/session-reaper, skills/handoff must carry EXACTLY ONE
  # "## Unattended-context gate" heading AND a description: field starting
  # with "Use when the user explicitly" (guards these destructive skills now
  # that DEC-R3 made every skill model-invocable + loop-able). Real failing
  # paths: a gate section deleted or duplicated, or a description that no
  # longer explicitly guards.
  local DESTRUCTIVE_SKILLS="push session-reaper handoff"
  local DESTR_OK=true ds dsf dsfm dsgc
  for ds in $DESTRUCTIVE_SKILLS; do
    dsf="$SKILL_DIR/$ds/SKILL.md"
    if [ ! -f "$dsf" ]; then
      DESTR_OK=false
      fail "S9 destructive gates" "$ds/SKILL.md: missing"
      continue
    fi
    dsgc=$(grep -c '^## Unattended-context gate$' "$dsf" 2>/dev/null)
    [ "$dsgc" -eq 1 ] || { DESTR_OK=false; fail "S9 destructive gates" "$ds: expected exactly 1 'Unattended-context gate' heading, found $dsgc"; }
    dsfm=$(sed -n '2,/^---$/p' "$dsf" 2>/dev/null)
    echo "$dsfm" | grep -qE '^description: *"?Use when the user explicitly' || { DESTR_OK=false; fail "S9 destructive gates" "$ds: description does not start with 'Use when the user explicitly'"; }
  done
  [ "$DESTR_OK" = true ] && pass "S9 destructive gates (push/session-reaper/handoff: exactly 1 gate heading + guarded description)"

  # S10: wf-skills schedule-driver skills (Wave-3). The 5 scheduled loop-driver skills
  # (wf-wave-monitor, wf-watchdog, wf-hpc-watch, wf-nb-watch, wf-hygiene) must each exist
  # with frontmatter carrying name, a description starting "Use when", category: workflow,
  # user-invocable: true, and NO disable-model-invocation key (DEC-R3), and must share the
  # R-1 6-section-header template (exactly 6 '## ' headers; the header TEXT varies by
  # driver, the COUNT does not). Real failing paths: a missing skill, a re-added flip key,
  # a wrong/absent category, or a header-count mismatch.
  local SCHED_SKILLS="wf-wave-monitor wf-watchdog wf-hpc-watch wf-nb-watch wf-hygiene"
  local SCHED_OK=true sks sksf sksfm skshc
  for sks in $SCHED_SKILLS; do
    sksf="$SKILL_DIR/$sks/SKILL.md"
    if [ ! -f "$sksf" ]; then
      SCHED_OK=false; fail "S10 schedule skills" "$sks/SKILL.md: missing"; continue
    fi
    sksfm=$(sed -n '2,/^---$/p' "$sksf" 2>/dev/null)
    echo "$sksfm" | grep -qE '^name:'                     || { SCHED_OK=false; fail "S10 schedule skills" "$sks: frontmatter missing name:"; }
    echo "$sksfm" | grep -qE '^description: *"?Use when'   || { SCHED_OK=false; fail "S10 schedule skills" "$sks: description must start with 'Use when'"; }
    echo "$sksfm" | grep -qE '^category: *workflow'        || { SCHED_OK=false; fail "S10 schedule skills" "$sks: category must be 'workflow'"; }
    echo "$sksfm" | grep -qE '^user-invocable: *true'      || { SCHED_OK=false; fail "S10 schedule skills" "$sks: user-invocable must be true"; }
    echo "$sksfm" | grep -qE '^disable-model-invocation:'  && { SCHED_OK=false; fail "S10 schedule skills" "$sks: disable-model-invocation must be absent (DEC-R3)"; }
    skshc=$(grep -c '^## ' "$sksf" 2>/dev/null)
    [ "$skshc" -eq 6 ] || { SCHED_OK=false; fail "S10 schedule skills" "$sks: R-1 template needs exactly 6 '## ' headers, found $skshc"; }
  done
  [ "$SCHED_OK" = true ] && pass "S10 schedule skills (5 wf-* drivers: Use-when desc, category workflow, user-invocable, no flip key, 6-section R-1 template)"

  # S11: wf-skills family invariants (Wave-3). EVERY skill named wf-* (the 3 flagship
  # drivers wf-design/wf-report/wf-websearch PLUS the 5 schedule drivers) must carry
  # category: workflow and must NOT carry a disable-model-invocation key. Discovered by
  # glob so a future wf-* is covered automatically; the family must number >= 8. Real
  # failing paths: a wf-* with a non-workflow category, a re-added flip key, or the family
  # shrinking below 8.
  local WFFAM_OK=true WF_N=0 wff wffname wfffm
  for wff in "$SKILL_DIR"/wf-*/SKILL.md; do
    [ -f "$wff" ] || continue
    WF_N=$((WF_N + 1))
    wffname=$(basename "$(dirname "$wff")")
    wfffm=$(sed -n '2,/^---$/p' "$wff" 2>/dev/null)
    echo "$wfffm" | grep -qE '^category: *workflow'        || { WFFAM_OK=false; fail "S11 wf-family" "$wffname: category must be 'workflow'"; }
    echo "$wfffm" | grep -qE '^disable-model-invocation:'  && { WFFAM_OK=false; fail "S11 wf-family" "$wffname: disable-model-invocation must be absent"; }
  done
  if [ "$WF_N" -lt 8 ]; then
    WFFAM_OK=false; fail "S11 wf-family" "expected >= 8 wf-* skills, found $WF_N"
  fi
  [ "$WFFAM_OK" = true ] && pass "S11 wf-family ($WF_N wf-* skills: all category workflow, none carry disable-model-invocation)"

  # S12: wf-skills loop-integration sections (Wave-3). The Class-A skills that gained a
  # /converge binding must retain their loop-integration section. The reviewer/checker/
  # driver set (heavy) and the light-variant set both carry a "## Loop integration"
  # heading; research/references/gap-audit.md is itself a /converge DRIVER and carries the
  # equivalent "### Convergence loop" section instead. Real failing path: a regression
  # stripping the loop-integration / convergence-loop section from any of them.
  local LOOP_HEAVY="fix-issue test-infra figure-validate sanity-check review design-review threat-model topology-producer-reviewer delegate hook-health better-super"
  local LOOP_LIGHT="mistake good-idea memory-prune plan pr brainstorm tdd recover-truncated hpc"
  local LOOP_OK=true lis lisf
  for lis in $LOOP_HEAVY $LOOP_LIGHT; do
    lisf="$SKILL_DIR/$lis/SKILL.md"
    if [ ! -f "$lisf" ]; then
      LOOP_OK=false; fail "S12 loop-integration" "$lis/SKILL.md: missing"; continue
    fi
    grep -qE '^## Loop integration' "$lisf" || { LOOP_OK=false; fail "S12 loop-integration" "$lis: missing '## Loop integration' section"; }
  done
  local GAP_AUDIT="$SKILL_DIR/research/references/gap-audit.md"
  if [ ! -f "$GAP_AUDIT" ]; then
    LOOP_OK=false; fail "S12 loop-integration" "research/references/gap-audit.md: missing"
  else
    grep -qE '^### Convergence loop' "$GAP_AUDIT" || { LOOP_OK=false; fail "S12 loop-integration" "gap-audit.md: missing '### Convergence loop' converge-driver section"; }
  fi
  [ "$LOOP_OK" = true ] && pass "S12 loop-integration (11 heavy + 9 light skills carry '## Loop integration'; gap-audit.md carries '### Convergence loop')"

  # S13: reviewer no-self-seal invariant (Wave-3). The three reviewer skills (review,
  # design-review, sanity-check) must state they never self-seal and must NOT contain a
  # positive self-seal construction. Positive check: each carries the literal "never seals
  # itself". Negative checks: no positive seal-attribution phrase ("is the seal" or "seals
  # the loop|round|artefact|convergence"), AND every "seals itself" occurrence is the
  # negated "never seals itself" (occurrence counts must match). Real failing paths: the
  # no-self-seal statement removed, an unnegated "seals itself" added, or a positive
  # "... is the seal" construction introduced. Verified clean against current files.
  local REVIEWERS="review design-review sanity-check"
  local SEAL_OK=true rvs rvsf rvs_all rvs_neg
  for rvs in $REVIEWERS; do
    rvsf="$SKILL_DIR/$rvs/SKILL.md"
    if [ ! -f "$rvsf" ]; then
      SEAL_OK=false; fail "S13 no-self-seal" "$rvs/SKILL.md: missing"; continue
    fi
    grep -qiE 'never seals itself' "$rvsf" || { SEAL_OK=false; fail "S13 no-self-seal" "$rvs: missing 'never seals itself' invariant"; }
    grep -qiE 'is the seal|seals the (loop|round|artefact|artifact|convergence)' "$rvsf" && { SEAL_OK=false; fail "S13 no-self-seal" "$rvs: positive self-seal phrase present"; }
    rvs_all=$(grep -oiE 'seals itself' "$rvsf" | wc -l | tr -d ' ')
    rvs_neg=$(grep -oiE 'never seals itself' "$rvsf" | wc -l | tr -d ' ')
    [ "$rvs_all" -eq "$rvs_neg" ] || { SEAL_OK=false; fail "S13 no-self-seal" "$rvs: unnegated 'seals itself' (total=$rvs_all negated=$rvs_neg)"; }
  done
  [ "$SEAL_OK" = true ] && pass "S13 no-self-seal (review/design-review/sanity-check: 'never seals itself' present, no positive self-seal construction)"
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
# WF: wf-skills Scripts Tests (comms + swarm + decontaminate)
# ═══════════════════════════════════════════════════
# New comms/swarm/firewall scripts landed by the wf-skills campaign (W1.3). All
# behavioral probes are hermetic: fixtures live under a throwaway $TMPDIR dir and
# the production broker DB / comms ledgers are NEVER touched or written.
test_wfscripts() {
  section "wf-skills Scripts Tests (WF)"

  local WF_SCRIPTS="comms/hcom-send.sh comms/broker-queries.sh decontaminate.sh swarm/recover-worker.sh"

  # WF1: all four scripts exist, are executable, and pass bash -n.
  local WF1_OK=true s sp
  for s in $WF_SCRIPTS; do
    sp="$SCRIPT_DIR/$s"
    if [ ! -f "$sp" ]; then WF1_OK=false; fail "WF1 scripts" "$s: missing"; continue; fi
    [ -x "$sp" ] || { WF1_OK=false; fail "WF1 scripts" "$s: not executable"; }
    bash -n "$sp" 2>/dev/null || { WF1_OK=false; fail "WF1 scripts" "$s: bash -n failed"; }
  done
  [ "$WF1_OK" = true ] && pass "WF1 wf-skills scripts (4 exist, executable, bash -n clean)"

  local WF_TMP
  WF_TMP=$(mktemp -d "${TMPDIR:-/tmp}/infratest-wf.XXXXXX" 2>/dev/null)

  # WF2: broker-queries.sh refuses an unrecognized verb with exit 1 (documented
  # read-only contract: "Any other verb is refused"). Hermetic: runs under a
  # throwaway HOME with a STUB broker.db so the default (verb-refusal) branch is
  # exercised WITHOUT reading the production broker (sqlite3 is never invoked for a
  # bogus verb, so the stub file's contents are irrelevant).
  local WF2_HOME="$WF_TMP/bh"
  mkdir -p "$WF2_HOME/.claude/comms"
  : > "$WF2_HOME/.claude/comms/.broker.db"
  HOME="$WF2_HOME" bash "$SCRIPT_DIR/comms/broker-queries.sh" __infratest_bogus_verb__ >/dev/null 2>&1
  local WF2_RC=$?
  if [ "$WF2_RC" -eq 1 ]; then
    pass "WF2 broker-queries.sh (unrecognized verb refused, exit 1)"
  else
    fail "WF2 broker-queries.sh" "bogus verb exit $WF2_RC (expected 1)"
  fi

  # WF3: decontaminate.sh firewall grep — a temp file containing a forbidden token
  # exits 1; a clean temp file exits 0. Fixtures MUST live outside ~/.claude/ (in
  # $TMPDIR), because decontaminate.sh EXEMPTS anything under ~/.claude/ — a fixture
  # there would false-pass. Never writes into the repo or any production ledger.
  local WF_FIX="$WF_TMP/fix"
  mkdir -p "$WF_FIX"
  printf 'this line references agent-memory internals\n' > "$WF_FIX/dirty.txt"
  printf 'this is ordinary clean project prose\n' > "$WF_FIX/clean.txt"
  bash "$SCRIPT_DIR/decontaminate.sh" "$WF_FIX/dirty.txt" >/dev/null 2>&1
  local WF3_DIRTY=$?
  bash "$SCRIPT_DIR/decontaminate.sh" "$WF_FIX/clean.txt" >/dev/null 2>&1
  local WF3_CLEAN=$?
  if [ "$WF3_DIRTY" -eq 1 ] && [ "$WF3_CLEAN" -eq 0 ]; then
    pass "WF3 decontaminate.sh (forbidden-token file exit 1, clean file exit 0)"
  else
    fail "WF3 decontaminate.sh" "dirty=$WF3_DIRTY (expect 1), clean=$WF3_CLEAN (expect 0)"
  fi

  # WF4: broker-queries.sh input-validation gate (M1 fix, wf-skills review round 1).
  # A crafted latest-rpt AGENT / volume SINCE arg shaped like a SQL-injection payload
  # (quote + semicolon) must be REJECTED before any sqlite3 invocation happens at all.
  # Hermetic: HOME points at a throwaway dir with a stub (empty) broker.db, so even a
  # validation regression could only ever touch the stub, never the real
  # ~/.claude/comms/.broker.db. Exit 2 is the script's dedicated validation-reject
  # code, distinct from sqlite3's own error exit (1) or success (0); this genuinely
  # fails if the M1 allowlist regresses, it does not just check for any nonzero code.
  local WF4_HOME="$WF_TMP/bh4"
  mkdir -p "$WF4_HOME/.claude/comms"
  : > "$WF4_HOME/.claude/comms/.broker.db"
  HOME="$WF4_HOME" bash "$SCRIPT_DIR/comms/broker-queries.sh" latest-rpt "x'; DROP TABLE messages;--" >/dev/null 2>&1
  local WF4_RPT_RC=$?
  HOME="$WF4_HOME" bash "$SCRIPT_DIR/comms/broker-queries.sh" volume "2026-01-01'; DELETE FROM messages;--" >/dev/null 2>&1
  local WF4_VOL_RC=$?
  if [ "$WF4_RPT_RC" -eq 2 ] && [ "$WF4_VOL_RC" -eq 2 ]; then
    pass "WF4 broker-queries.sh (SQLi-shaped args rejected pre-SQL, exit 2)"
  else
    fail "WF4 broker-queries.sh" "latest-rpt exit $WF4_RPT_RC, volume exit $WF4_VOL_RC (expected 2, 2)"
  fi

  rm -rf "$WF_TMP" 2>/dev/null
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
    wfscripts|wf|WF)    test_wfscripts ;;
    *)
      echo "Unknown component: $COMPONENT"
      echo "Valid: hooks, settings, agents, skills, rules, comms, matrix, skm, wfscripts"
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
  test_wfscripts
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
