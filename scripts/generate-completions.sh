#!/usr/bin/env bash
# Generates claude-completions.txt from user-invocable skills in ~/.claude/skills/
# Usage: bash ~/.claude/scripts/generate-completions.sh

SKILLS_DIR="$HOME/.claude/skills"
OUTPUT="$HOME/.claude/scripts/claude-completions.txt"

{
  echo "# Auto-generated from ~/.claude/skills/ — do not edit manually"
  echo "# Regenerate: bash ~/.claude/scripts/generate-completions.sh"
  echo "# Usage: rlwrap -f ~/.claude/scripts/claude-completions.txt claude"
  echo ""
  for dir in "$SKILLS_DIR"/*/; do
    skill_file="$dir/SKILL.md"
    [ -f "$skill_file" ] || continue
    # Only include user-invocable skills
    if grep -q 'user-invocable: true' "$skill_file" 2>/dev/null; then
      basename "$dir"
    fi
  done | sort | sed 's/^/\//'
} > "$OUTPUT"

echo "Generated $(grep -c '^/' "$OUTPUT") completions -> $OUTPUT"
