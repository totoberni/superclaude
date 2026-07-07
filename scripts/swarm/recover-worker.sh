#!/usr/bin/env bash
# recover-worker.sh - forensic extraction from Claude Code worker artifacts,
# for /recover-truncated (or manual invocation), when a background task or
# subagent needs a compact summary pulled out of its raw output without
# dumping raw JSONL into an LLM context window.
#
# Usage:
#   recover-worker.sh task <output-file>
#     <output-file> is a background task output file, pattern:
#     .../tasks/<task-id>.output
#
#   recover-worker.sh agent <jsonl-file>
#     <jsonl-file> is a subagent transcript, pattern:
#     ~/.claude/projects/<project-slug>/<session-id>/subagents/agent-<agentId>.jsonl
#
# Both modes hard-cap stdout to roughly 3000 characters total and never
# print an entire raw JSONL file. jq is required for structured extraction;
# every jq step has a non-jq fallback for malformed or truncated input.

# NOTE: intentionally no -e (errexit). Every fallible step below has an
# explicit fallback path; -e would abort mid-chain on the first non-zero
# jq exit instead of falling through to the next fallback.
set -uo pipefail

STDOUT_CAP=3000
AGENT_TEXT_CAP=2000

err() {
  echo "ERROR: $1" >&2
}

usage() {
  echo "usage: recover-worker.sh task <output-file>" >&2
  echo "       recover-worker.sh agent <jsonl-file>" >&2
}

# cap <max-chars>: read stdin, truncate to max-chars, note if truncated.
cap() {
  local max="$1"
  local input
  input="$(cat)"
  local len=${#input}
  if [ "$len" -gt "$max" ]; then
    printf '%s\n[... truncated at %d of %d chars ...]\n' "${input:0:$max}" "$max" "$len"
  else
    printf '%s\n' "$input"
  fi
}

# mode_task <output-file>: print a header + the final result text.
mode_task() {
  local file="$1"
  [ -f "$file" ] || { err "task output file not found: $file"; return 1; }

  local result=""
  if command -v jq >/dev/null 2>&1; then
    result="$(tail -n 1 "$file" 2>/dev/null | jq -r '.message?.content?[0]?.text? // empty' 2>/dev/null)"
    if [ -z "$result" ]; then
      # Fallback: .message.content may be a bare string, not an array.
      result="$(tail -n 1 "$file" 2>/dev/null | jq -r 'if (.message?.content? | type) == "string" then .message.content else empty end' 2>/dev/null)"
    fi
  fi

  if [ -z "$result" ]; then
    # Fallback: no jq, or jq found nothing usable in the last line.
    # Walk back from the end for the last non-empty raw line.
    result="$(tac "$file" 2>/dev/null | grep -m 1 -v '^[[:space:]]*$')"
  fi

  [ -n "$result" ] || { err "could not extract a result from $file"; return 1; }

  echo "## Task output recovery: $file"
  echo ""
  printf '%s\n' "$result"
}

# mode_agent <jsonl-file>: print last assistant text, tool_use count, and
# deduplicated Write/Edit file paths.
mode_agent() {
  local file="$1"
  [ -f "$file" ] || { err "agent transcript file not found: $file"; return 1; }
  command -v jq >/dev/null 2>&1 || { err "jq is required for agent mode and was not found on PATH"; return 1; }

  # Stage 1: tolerate a truncated/corrupt final line by parsing line-by-line
  # and dropping anything that does not parse (fromjson?), rather than one
  # jq -s slurp that would fail outright on a single bad line.
  # Stage 2: slurp the now-guaranteed-valid objects and aggregate.
  local agg
  agg="$(jq -Rc 'fromjson? // empty' "$file" 2>/dev/null | jq -cs '
      [ .[] | select(.type == "assistant") | .message?.content?[]? | select(.type == "text") | .text? ] as $texts
    | [ .[] | select(.type == "assistant") | .message?.content?[]? | select(.type == "tool_use") ] as $tool_uses
    | {
        last_text: (($texts | last) // ""),
        tool_use_count: ($tool_uses | length),
        file_paths: ( [ $tool_uses[] | select(.name == "Write" or .name == "Edit") | (.input?.file_path? // empty) ] | unique )
      }
  ' 2>/dev/null)"

  local last_text="" tool_use_count="" file_paths=""
  if [ -n "$agg" ]; then
    last_text="$(printf '%s' "$agg" | jq -r '.last_text // empty' 2>/dev/null)"
    tool_use_count="$(printf '%s' "$agg" | jq -r '.tool_use_count // empty' 2>/dev/null)"
    file_paths="$(printf '%s' "$agg" | jq -r '.file_paths[]?' 2>/dev/null)"
  fi

  if [ -z "$last_text" ]; then
    # Fallback: last non-empty raw line, in case the aggregation above
    # found no assistant text block at all (unexpected transcript shape).
    last_text="$(tac "$file" 2>/dev/null | grep -m 1 -v '^[[:space:]]*$')"
    [ -n "$last_text" ] || last_text="(no assistant text message found)"
  fi
  [ -n "$tool_use_count" ] || tool_use_count="unknown"

  echo "## Agent transcript recovery: $file"
  echo ""
  echo "### Last assistant text message"
  printf '%s\n' "$last_text" | cap "$AGENT_TEXT_CAP"
  echo ""
  echo "### tool_use event count"
  echo "$tool_use_count"
  echo ""
  echo "### Write/Edit file paths touched (deduplicated)"
  if [ -n "$file_paths" ]; then
    printf '%s\n' "$file_paths"
  else
    echo "(none)"
  fi
}

main() {
  local mode="${1:-}"
  local target="${2:-}"

  case "$mode" in
    task|agent) ;;
    *)
      usage
      exit 1
      ;;
  esac

  [ -n "$target" ] || { usage; exit 1; }

  local output rc
  if [ "$mode" = "task" ]; then
    output="$(mode_task "$target")"
    rc=$?
  else
    output="$(mode_agent "$target")"
    rc=$?
  fi

  [ "$rc" -eq 0 ] || exit "$rc"

  printf '%s\n' "$output" | cap "$STDOUT_CAP"
}

main "$@"
