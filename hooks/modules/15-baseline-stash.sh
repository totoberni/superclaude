# Module: Baseline-stash — auto-stash baseline (git status + diff) at session start
# for repos where the project policy is /commit false.
# Reads: TOOL_NAME, INPUT, SESSION_ID, TIMER_DIR
# Mitigates R-2: w-reviewer dirty-tree attribution gotcha (reviewers blame current
# wave for pre-existing changes when no clean commit baseline exists).
#
# One-shot per session: marker file pattern (matches 05-context-check.sh style).
# Fires on the first Edit/Write/MultiEdit tool call this session.

mod_baseline_stash() {
  # Only fire on tools that produce dirty diffs
  case "$TOOL_NAME" in
    Edit|Write|MultiEdit) ;;
    *) return 0 ;;
  esac

  # One-shot per session
  local MARKER="$TIMER_DIR/${SESSION_ID}.baseline-stashed"
  [ -f "$MARKER" ] && return 0

  # Resolve current working directory (where the agent is operating)
  local CWD
  CWD=$(pwd 2>/dev/null) || return 0

  # ── Detect /commit false policy ──
  # Heuristic 1: env var override (highest precedence)
  # Heuristic 2: cwd basename matches a project listed in no-commit-projects.local
  local COMMIT_POLICY="${CLAUDE_COMMIT_POLICY:-true}"

  # Load no-commit project list from gitignored config (one basename per line).
  # If the file is absent the list is empty and the hook becomes a safe no-op.
  local CONFIG_FILE="$HOME/.claude/hooks/no-commit-projects.local"
  local PROJECTS_NO_COMMIT=()
  if [ -f "$CONFIG_FILE" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      # Skip blank lines and comment lines
      [[ -z "$line" || "$line" == "#"* ]] && continue
      PROJECTS_NO_COMMIT+=("$line")
    done < "$CONFIG_FILE"
  fi

  local IS_NO_COMMIT=false

  if [ "$COMMIT_POLICY" = "false" ]; then
    IS_NO_COMMIT=true
  else
    local PROJ
    for PROJ in "${PROJECTS_NO_COMMIT[@]}"; do
      case "$CWD" in
        *"/$PROJ"|*"/$PROJ/"*) IS_NO_COMMIT=true; break ;;
      esac
    done
  fi

  # Not a /commit false repo — set marker so we don't re-check on every tool call
  if [ "$IS_NO_COMMIT" != "true" ]; then
    touch "$MARKER"
    return 0
  fi

  # ── Find git root ──
  local REPO
  REPO=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) || { touch "$MARKER"; return 0; }

  # ── Capture baseline ──
  local BASELINE_STATUS="/tmp/${SESSION_ID}-baseline.txt"
  local BASELINE_DIFF="/tmp/${SESSION_ID}-baseline.diff"
  git -C "$REPO" status --short > "$BASELINE_STATUS" 2>/dev/null || true
  git -C "$REPO" diff > "$BASELINE_DIFF" 2>/dev/null || true

  touch "$MARKER"

  # Emit additionalContext so reviewers know the baseline location
  emit_context "R-2 BASELINE STASHED for /commit false repo. Pre-existing changes (NOT this wave): see $BASELINE_STATUS and $BASELINE_DIFF. Inject these paths into any w-reviewer dispatch prompt to avoid false-positive REJECTs."
}
