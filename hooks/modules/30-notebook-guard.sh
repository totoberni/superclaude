# Module: Notebook guard — PreToolUse hard-block (exit 2) for banned .ipynb patterns
# Reads: TOOL_NAME, INPUT
# Hard enforcement (not soft like 25-commit-gate.sh) — per the user's choice 4 ("hard block").

mod_notebook_guard() {
  case "$TOOL_NAME" in
    NotebookEdit)
      printf 'BLOCKED: NotebookEdit is shadow-deprecated by /notebook skill (atomicity policy: ALL .ipynb writes go through `nb batch`). See ~/.claude/skills/notebook/SKILL.md.\n' >&2
      exit 2
      ;;
    Edit|Write)
      FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null) || FILE_PATH=""
      if echo "$FILE_PATH" | grep -qE '\.ipynb$'; then
        printf 'BLOCKED: Direct Edit/Write on .ipynb is forbidden. Use `nb batch <nb> --plan <plan.yml>` for structural changes, or edit the paired .py for source-only edits. See ~/.claude/skills/notebook/SKILL.md.\n' >&2
        exit 2
      fi
      # Also block raw json/nbformat patterns aimed at .ipynb — but ONLY in
      # newly-written/edited .py files. Documentation files (.md, .txt, etc.)
      # legitimately reference the banned patterns when describing the ban.
      if echo "$FILE_PATH" | grep -qE '\.py$'; then
        CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // .tool_input.new_string // ""' 2>/dev/null) || CONTENT=""
        if [ -n "$CONTENT" ]; then
          # Catch raw json on .ipynb in either order (json.load BEFORE or AFTER `.ipynb`).
          if echo "$CONTENT" | grep -qE 'json\.(load|dump)' && echo "$CONTENT" | grep -qE '\.ipynb'; then
            printf 'BLOCKED: raw json on .ipynb detected in %s. Use the /notebook skill (nb_io.atomic_write_ipynb / nb_io.load_ipynb). See SKILL.md.\n' "$FILE_PATH" >&2
            exit 2
          fi
          # Catch nbformat.write(open(...)) — direct non-atomic write.
          if echo "$CONTENT" | grep -qE 'nbformat\.write\([^)]*open\('; then
            printf 'BLOCKED: raw nbformat.write(open(...)) detected in %s. Use atomic_write_ipynb. See SKILL.md.\n' "$FILE_PATH" >&2
            exit 2
          fi
        fi
      fi
      ;;
    Bash)
      BASH_CMD=$(get_bash_cmd "$INPUT")
      [ -z "$BASH_CMD" ] && return 0
      # Block direct nbconvert --inplace on .ipynb (must go through `nb execute`).
      if echo "$BASH_CMD" | grep -qE 'jupyter\s+nbconvert.*--inplace.*\.ipynb'; then
        printf 'BLOCKED: `jupyter nbconvert --inplace` on .ipynb bypasses skill atomic-write + lock. Use `nb execute <nb>`.\n' >&2
        exit 2
      fi
      # Block raw nbformat.write / json.dump on .ipynb in shell-driven Python
      # one-liners. Both alternatives require `python -c` (or python3) actually
      # invoked — avoids false positives on echo'd test fixtures or commentary
      # that mentions the patterns without executing them.
      if echo "$BASH_CMD" | grep -qE 'python[0-9]*\s+(-c|-m)\s'; then
        if echo "$BASH_CMD" | grep -qE 'nbformat\.write\([^)]*open\('; then
          printf 'BLOCKED: raw nbformat.write(open(...)) in shell command. Use `nb batch`.\n' >&2
          exit 2
        fi
        if echo "$BASH_CMD" | grep -qE 'json\.(load|dump)' \
            && echo "$BASH_CMD" | grep -qE '\.ipynb'; then
          printf 'BLOCKED: raw json on .ipynb in shell command. Use `nb batch`.\n' >&2
          exit 2
        fi
      fi
      ;;
  esac
}
