---
name: nudge
description: "Use when probing agent status, self or a named orch."
model: haiku
category: orchestration
user-invocable: true
argument-hint: "<max-chars> [target-agent]"
allowed-tools: Read, Bash, Glob, Write
---

# Nudge — Quick Status Probe

Fast status check for the user. Two modes depending on arguments.

**Arguments**: $ARGUMENTS — parse as `<max-chars> [target-agent]`

- If only a number: **self-nudge** (respond immediately)
- If number + agent name: **cross-nudge** (queue for target via hook)
- If empty: default to self-nudge with 280 char limit

## Self-Nudge (no target agent)

Immediately produce a status report in chat. Do NOT write to a file — output directly in the conversation.

### Steps

1. Determine your agent name (from your system prompt or `--agent` flag)
2. Find your elapsed time:
   ```bash
   # Find your session timer start file
   for f in $HOME/.claude/session-timers/*.start; do
     [ -f "$f" ] && echo "$(basename "$f" .start): $(cat "$f")"
   done
   ```
   Calculate elapsed = `$(date +%s)` minus start time. Format as `Xm` or `Xh Ym`.
3. Determine your status emoji:
   - `✅` — OK, progressing normally
   - `🔄` — waiting on something (test run, background task)
   - `❌` — stuck or hit an error
   - `⏳` — wrapping up / in grace period
4. Read your state file to understand current task:
   - Check `~/.claude/plans/*/state*.md` matching your orch name
   - Or your most recent report in `~/.claude/comms/<your-name>/reports.md`
5. Output in this exact format:

```
[NUDGE] <agent-name> | <elapsed-time> | <status-emoji>
<description: what you're doing, what's blocking, ETA — max N chars>
```

The header line does NOT count toward the char limit. Only the description line must be ≤ N chars.

**Be terse.** This is a quick pulse check, not a report.

## Cross-Nudge (target agent specified)

Queue a nudge for another agent. The target's `session-timer.sh` hook will deliver it as `additionalContext` on their next tool call — the agent cannot ignore it.

### Steps

1. Parse the target agent name from $ARGUMENTS (second word)
2. Determine source agent name (your own `--agent` / system-prompt identity); fall back to `unknown`.
3. Write the flat-file nudge (canonical path — `session-timer.sh` reads from here):
   ```bash
   mkdir -p $HOME/.claude/nudge
   echo "<max-chars>" > "$HOME/.claude/nudge/<target-agent-name>"
   ```
4. **HCOM dual-write** (Phase B) — mirror to SQLite broker for queryability + `hcom-pre-tool-use.sh` injection:
   ```bash
   hcom_send "$source_agent" "@<target-agent-name>" "NUDGE" "" "<max-chars> char status probe"
   ```
   See "## HCOM Dual-Write" below for the helper definition.
5. Confirm to the user:
   > Nudge queued for `<target>`. It will fire automatically on their next tool call (no Esc needed — the hook delivers it as `additionalContext` which the agent cannot ignore).

The hook handles delivery and cleanup automatically. Both delivery channels (flat-file and SQLite) are checked by the receiving agent's hooks.

## HCOM Dual-Write (Phase B)

Cross-nudge also sends a `NUDGE` message to the HCOM SQLite broker (`~/.claude/comms/.broker.db`). Pattern: **flat-file write first (canonical, consumed by `session-timer.sh`), SQLite write second (fail-soft mirror, consumed by `hcom-pre-tool-use.sh`)**.

```bash
hcom_send() {
  # args: from_agent  to_agent  kind  seq(optional)  body
  local from="$1" to="$2" kind="$3" seq="$4" body="$5"
  "$HOME/.claude/.venv/bin/python" "$HOME/.claude/scripts/hcom-broker.py" send \
    --from "$from" --to "$to" --kind "$kind" \
    ${seq:+--seq "$seq"} \
    --body "$body" 2>/dev/null \
    || echo "Warning: HCOM send failed (broker unavailable)" >&2
}
```

Rules:
- Flat-file nudge MUST be written first; SQLite mirror is opt-in and fail-soft.
- Self-nudge mode does NOT dual-write (no recipient — output is direct chat).
- If the broker DB is missing or fails: log to stderr and continue.
- Kind for cross-nudge is always `NUDGE`. Body is the char-limit or short status probe message; the user can also pass a custom probe string in future revisions.

## Examples

- `/nudge` → self-nudge, 280 char limit
- `/nudge 200` → self-nudge, 200 char limit
- `/nudge 200 orch-pciss-p3` → cross-nudge to orch-pciss-p3, 200 char limit
- `/nudge 100 scaf` → cross-nudge to scaf, 100 char limit
