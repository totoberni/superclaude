# Module: Agent thinking-keyword nudge (V-001 THINKING-MISSING mitigation)
# Reads: TOOL_NAME, INPUT, SESSION_ID, TIMER_DIR
# Side effect: emits a system-reminder via additionalContext JSON when matrix prescribes
# a thinking keyword for the dispatched subagent_type but the spawn prompt lacks one.
#
# NON-BLOCKING — never prevents dispatch, never exits non-zero. Soft advisory only.
#
# Source: V-001 retro lesson #1 (model selection ≠ thinking depth) + W-7 best practice
# that subagent thinking is NOT inherited from parent (rules/13-worker-first-mandate.md
# § Critical Implementation Note).
#
# Slot 14 — between 10-nudge and 15-baseline-stash; pre-tool-use phase.

mod_thinking_nudge() {
  [ "$TOOL_NAME" = "Agent" ] || return 0

  # Parse subagent_type and spawn prompt
  local SUBAGENT PROMPT
  SUBAGENT=$(echo "$INPUT" | jq -r '.tool_input.subagent_type // ""' 2>/dev/null) || SUBAGENT=""
  PROMPT=$(echo "$INPUT" | jq -r '.tool_input.prompt // ""' 2>/dev/null) || PROMPT=""

  [ -z "$SUBAGENT" ] && return 0
  [ -z "$PROMPT" ] && return 0

  # Check if prompt already embeds any thinking keyword (case-insensitive, word-boundary).
  # Order longest-first so "think harder" matches before "think".
  if echo "$PROMPT" | grep -qiE '\b(ultrathink|megathink|think harder|think hard|think)\b' 2>/dev/null; then
    return 0   # Already embedded — nothing to nudge about
  fi

  # ── Matrix mapping (canonical: rules/13-worker-first-mandate.md § Per-Worker Defaults) ──
  # Nudge when:
  #   - subagent_type has a matrix-prescribed keyword for COMMON task shapes, AND
  #   - prompt lacks any thinking keyword.
  # Some types are conditional on task-class signals in the prompt.

  local PRESCRIBED=""
  local TASK_CLASS=""

  # Lowercase the prompt once for substring checks
  local P_LOWER
  P_LOWER=$(printf '%s' "$PROMPT" | tr '[:upper:]' '[:lower:]')

  case "$SUBAGENT" in
    w-debugger)
      # Single-file → think; multi-file/race → think harder.
      # Signal "race", "multi-file", "across" implies multi-file.
      if echo "$P_LOWER" | grep -qE '\b(race condition|race-condition|multi-file|across (files|modules)|cross-file)\b'; then
        PRESCRIBED="think harder"
        TASK_CLASS="multi-file race / cross-file"
      else
        PRESCRIBED="think"
        TASK_CLASS="single-file debug"
      fi
      ;;
    w-planner)
      # Architectural → ultrathink; otherwise → think hard.
      if echo "$P_LOWER" | grep -qE '\b(architect|architectural|architecture|3\+ phase|multi-phase|irreversible)\b'; then
        PRESCRIBED="ultrathink"
        TASK_CLASS="architectural / multi-phase planning"
      else
        PRESCRIBED="think hard"
        TASK_CLASS="single-phase planning"
      fi
      ;;
    w-reviewer)
      # Only nudge when scathingly-deep or "deep" / "architectural review" mentioned.
      if echo "$P_LOWER" | grep -qE '(scathingly[- ]deep|--scathingly-deep|architectural review|deep review)'; then
        PRESCRIBED="think hard"
        TASK_CLASS="scathingly-deep / architectural review"
      fi
      ;;
    w-doc)
      # Cross-section coherence → think (per matrix); single-section polish → no nudge.
      if echo "$P_LOWER" | grep -qE '\b(cross[- ]section|coherence|architectural|cross-cutting|consistency across)\b'; then
        PRESCRIBED="think"
        TASK_CLASS="cross-section / coherence doc work"
      fi
      ;;
    w-implementer)
      # >5 files or architecture → think hard; ≤3 files → no nudge.
      # Detect explicit file-count hints (e.g., "5 files", "10 files") or architecture signal.
      local FILE_COUNT
      FILE_COUNT=$(printf '%s' "$P_LOWER" | grep -oE '\b([5-9]|[1-9][0-9])\s*(files|modules|components)\b' | head -1)
      if [ -n "$FILE_COUNT" ] || echo "$P_LOWER" | grep -qE '\b(architect|architecture|cross-cutting|web app|whole-app)\b'; then
        PRESCRIBED="think hard"
        TASK_CLASS="multi-file / architectural implementation"
      fi
      ;;
    w-merger)
      # Semantic conflict or race → think hard.
      if echo "$P_LOWER" | grep -qE '\b(semantic|race|three-way|complex conflict)\b'; then
        PRESCRIBED="think hard"
        TASK_CLASS="semantic merge conflict"
      fi
      ;;
    # No nudge for: general-purpose, w-explorer, w-committer, w-tester, w-refactorer (matrix has no default)
    *) return 0 ;;
  esac

  [ -z "$PRESCRIBED" ] && return 0

  # One-shot per (session, subagent_type) — avoid nudge spam on rapid parallel dispatches
  local NUDGE_MARKER="$TIMER_DIR/${SESSION_ID}.thinking-nudge-${SUBAGENT}"
  [ -f "$NUDGE_MARKER" ] && return 0
  touch "$NUDGE_MARKER" 2>/dev/null || true

  emit_context "Matrix prescribes \`${PRESCRIBED}\` for ${SUBAGENT} on ${TASK_CLASS} tasks. Consider embedding the keyword in the spawn prompt (subagent thinking is NOT inherited from parent — see rules/13-worker-first-mandate.md § Critical Implementation Note). This is advisory only — proceeding with dispatch as-is."

  return 0
}
