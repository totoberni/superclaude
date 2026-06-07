#!/usr/bin/env bash
# Hook: comms-schema-lint
# Event: PostToolUse for Edit|Write|MultiEdit on ~/.claude/comms/*/{directives,reports,escalations}.md
# Purpose: Soft-lint that the latest entry headers match the expected schema (HCOM-prep).
# Soft = warn via additionalContext, never block.
# Schema source: ~/.claude/comms/README.md §Message Formats
# HCOM design:   ~/.claude/docs/hcom-design.md

set -uo pipefail

# Read hook payload from stdin (JSON envelope from Claude Code)
input=$(cat)
tool_name=$(echo "$input" | jq -r '.tool_name // ""' 2>/dev/null || echo "")
file_path=$(echo "$input" | jq -r '.tool_input.file_path // ""' 2>/dev/null || echo "")

# Only act on file-mutating tools
case "$tool_name" in
  Edit|Write|MultiEdit) ;;
  *) exit 0 ;;
esac

# Only act on the three comms ledger files
case "$file_path" in
  *"/comms/"*"/directives.md") prefix="DIR" ;;
  *"/comms/"*"/reports.md")    prefix="RPT" ;;
  *"/comms/"*"/escalations.md") prefix="ESC" ;;
  *) exit 0 ;;
esac

# Bail if the file vanished (rare race)
[ -f "$file_path" ] || exit 0

# Find the latest "## <prefix>-NNN" header line
latest_line=$(grep -n "^## ${prefix}-[0-9]" "$file_path" 2>/dev/null | tail -1 | cut -d: -f1)
[ -z "$latest_line" ] && exit 0

# Read up to 12 lines after the header — enough to cover the standard 5-7 field block
chunk=$(sed -n "${latest_line},$((latest_line+12))p" "$file_path" 2>/dev/null)
[ -z "$chunk" ] && exit 0

# Required fields per kind (per ~/.claude/comms/README.md §Message Formats)
missing=()
echo "$chunk" | grep -q "^\*\*Time\*\*:" || missing+=("Time")

case "$prefix" in
  DIR)
    echo "$chunk" | grep -q "^\*\*Project\*\*:" || missing+=("Project")
    ;;
  RPT)
    echo "$chunk" | grep -q "^\*\*Directive\*\*:" || missing+=("Directive")
    echo "$chunk" | grep -q "^\*\*Status\*\*:" || missing+=("Status")
    ;;
  ESC)
    echo "$chunk" | grep -q "^\*\*Context\*\*:" || missing+=("Context")
    ;;
esac

# Soft warning via additionalContext — never blocks the tool call
if [ "${#missing[@]}" -gt 0 ]; then
  miss_csv=$(IFS=,; echo "${missing[*]}")
  rel_path="$(basename "$(dirname "$file_path")")/$(basename "$file_path")"
  jq -nc \
    --arg msg "comms-schema-lint: latest ${prefix} entry in ${rel_path} is missing required fields: ${miss_csv}. Expected schema per ~/.claude/comms/README.md §Message Formats. HCOM-prep: SQLite migration (see ~/.claude/docs/hcom-design.md) parses these fields directly — entries without them won't ingest cleanly." \
    '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $msg}}'
fi

exit 0
