#!/usr/bin/env bash
# Hook: memory-inject   — wire to BOTH SessionStart AND SubagentStart.
# Purpose: inject an IMPERATIVE DB-memory pointer (and, for top-level sessions,
#          an agent-scoped recovery slice) so that top-level agents AND subagents
#          query the SQLite memory DB instead of legacy MD files.
#
# Why both events: SessionStart fires only for top-level sessions; subagents fire
# SubagentStart (they never see SessionStart, and they do NOT receive the binary's
# "# Persistent Agent Memory" block / CLAUDE_COWORK_MEMORY_GUIDELINES). This hook is
# the only mechanism that reliably reaches subagents with a memory instruction.
#
# FAIL-SAFE CONTRACT (this becomes LIVE on every session + every subagent spawn):
#   - set -uo pipefail (NOT set -e). Every path ends: exit 0. Never blocks. <500ms.
#   - jq/python/DB failure → still emit the static pointer; worst case emit {}.
#   - NEVER emit invalid JSON, NEVER hang. No $HOME hardcoding.

set -uo pipefail

INPUT=$(cat 2>/dev/null || true)

# --- 0. Parse event name (echoed back) + agent identity (jq, grep fallback) ----
EVENT="SessionStart"
AGENT="meta"
if command -v jq >/dev/null 2>&1; then
    E=$(printf '%s' "$INPUT" | jq -r '.hook_event_name // ""' 2>/dev/null || true)
    [ -n "$E" ] && EVENT="$E"
    A=$(printf '%s' "$INPUT" | jq -r '.agent_type // ""' 2>/dev/null || true)
    [ -n "$A" ] && AGENT="$A"
else
    E=$(printf '%s' "$INPUT" | grep -oE '"hook_event_name"[[:space:]]*:[[:space:]]*"[^"]+"' 2>/dev/null \
         | sed 's/.*"\([^"]*\)"[[:space:]]*$/\1/' || true)
    [ -n "$E" ] && EVENT="$E"
    A=$(printf '%s' "$INPUT" | grep -oE '"agent_type"[[:space:]]*:[[:space:]]*"[^"]+"' 2>/dev/null \
         | sed 's/.*"\([^"]*\)"[[:space:]]*$/\1/' || true)
    [ -n "$A" ] && AGENT="$A"
fi

# --- 1. Imperative pointer (always injected; the behavior trigger) -------------
# Imperative phrasing is deliberate: a passive "memory is available" did not make
# subagents query. "BEFORE you act, FIRST run ..." does.
POINTER="MEMORY (read this first): your persistent memory is a hybrid-search SQLite DB at ~/.claude/agent-memory/.memory.db — there are NO MD memory files to read. BEFORE you rely on any assumption about a project, library, tool, convention, or past decision, FIRST query it:
  HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py search \"<your topic> gotchas\" -k 6
then fetch a full entry with: memory_db.py get --name <slug>; to find memories related to one you already hold (near-dups, or the same topic worded differently), use: memory_db.py similar --name <slug>. Tiers: instance/<agent>, shared-projects, shared-global, class. To write/update memory, use the memory skills (/remember, /good-idea, /lt-mem, /mistake) — never hand-write .md memory files. (See rules/12 § Memory Access for detail.)"

# --- 2. Agent-scoped recovery slice — top-level (SessionStart) only ------------
# Subagents are task-scoped; a generic slice is noise for them, so we keep their
# injection to the imperative pointer (also near-instant: no python call).
SLICE=""
if [ "$EVENT" = "SessionStart" ]; then
    PYTHON="$HOME/.claude/.venv/bin/python"
    MDB="$HOME/.claude/scripts/memory/memory_db.py"
    if [ -x "$PYTHON" ] && [ -f "$MDB" ]; then
        RAW=$(
            timeout 5 env HF_HUB_OFFLINE=1 "$PYTHON" "$MDB" \
                search "$AGENT recovery context current state handoff" --mode fts -k 4 2>/dev/null
        ) || true
        [ -n "$RAW" ] && SLICE=$(printf '%s' "$RAW" | head -c 4000 || true)
    fi
fi

# --- 3. Assemble additionalContext (cap < 10000) ------------------------------
if [ -n "$SLICE" ]; then
    CTX="${POINTER}

Most relevant stored memories for agent=${AGENT} (query the DB for more):
${SLICE}"
else
    CTX="$POINTER"
fi
CTX=$(printf '%s' "$CTX" | head -c 9800 || true)

# --- 4. Emit (echo the event name back). jq handles escaping; {} as last resort.
if command -v jq >/dev/null 2>&1; then
    jq -nc \
        --arg ev "$EVENT" \
        --arg ctx "$CTX" \
        '{"hookSpecificOutput":{"hookEventName":$ev,"additionalContext":$ctx}}' \
        2>/dev/null || printf '{}'
else
    printf '{}'
fi

exit 0
