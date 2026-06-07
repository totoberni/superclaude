# Module: Commit quality gate — conventional commit format + push reminder
# Reads: TOOL_NAME, INPUT, NUDGE_FIRED
# Soft enforcement ONLY — never blocks commits

mod_commit_gate() {
  [ "$TOOL_NAME" = "Bash" ] || return 0
  [ "$NUDGE_FIRED" = true ] && return 0

  BASH_CMD=$(get_bash_cmd "$INPUT")
  [ -z "$BASH_CMD" ] && return 0

  # ── Push reminder ──
  if echo "$BASH_CMD" | grep -qE '^\s*git(\s+-C\s+\S+)?\s+push'; then
    emit_context "Push detected. Reminder: verify with the user before pushing. Never push without explicit instruction."
    return 0
  fi

  # ── Commit format check ──
  # Match: git commit, git -C <path> commit (but NOT git log --grep=commit, etc.)
  if ! echo "$BASH_CMD" | grep -qE '^\s*git(\s+-C\s+\S+)?\s+commit'; then
    return 0
  fi

  # Skip --amend (message already exists)
  if echo "$BASH_CMD" | grep -q -- '--amend'; then
    return 0
  fi

  # Extract commit message from -m flag (handles heredoc and quoted strings)
  COMMIT_MSG=""
  if echo "$BASH_CMD" | grep -qE '\-m\s'; then
    # Try to get the message after -m (first line is what matters for prefix)
    COMMIT_MSG=$(echo "$BASH_CMD" | grep -oP -- '-m\s+["'"'"']?\K[^"'"'"']+' 2>/dev/null | head -1) || COMMIT_MSG=""
    # Also handle heredoc: look for first non-empty line after EOF marker
    if [ -z "$COMMIT_MSG" ]; then
      COMMIT_MSG=$(echo "$BASH_CMD" | grep -oP -- '-m\s+.*?<<.*?EOF\n\K[^\n]+' 2>/dev/null | head -1) || COMMIT_MSG=""
    fi
  fi

  # If we couldn't extract the message, skip (don't false-positive)
  [ -z "$COMMIT_MSG" ] && return 0

  # Check conventional commit prefix
  if ! echo "$COMMIT_MSG" | grep -qE '^\s*(feat|fix|test|docs|chore|refactor|style|ci|perf|build)(\(.+\))?!?:'; then
    # One-shot per session (audit O15): same agent gets ONE conventional-format
    # warning per session — they've heard the message, no need to repeat. The
    # marker key is plain "commit-gate" (not per-message hashed) because
    # repeated warnings inside the same session are noise; the agent will see
    # other failures (rejected commits, hook output) if they ignore this one.
    if ! already_warned "$SESSION_ID" "commit-gate"; then
      emit_context "Commit message may not follow conventional format. Expected: feat:|fix:|test:|docs:|chore:|refactor:|style:|ci:|perf:|build: prefix. Current: $(echo "$COMMIT_MSG" | head -c 60)"
    fi
  fi
}
