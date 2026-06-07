# Hook Ordering Audit (post-H5)

**Date**: 2026-05-09
**Trigger**: H5 web research found Claude Code runs hooks on the same event in PARALLEL with no defined order. Numeric prefix in `modules/NN-name.sh` is cosmetic.

**Sources**:
- github.com/anthropics/claude-code/issues/21533 — sequential-execution proposal closed "not planned" 2026-01-28
- blakecrosley.com production-pattern docs — recommend single wrapper script for ordering OR fully independent hooks (own state, no shared reads)

## Findings

### Hooks per event (verified against `~/.claude/settings.json`)

| Event | Hooks fired (parallel) | Notes |
|-------|------------------------|-------|
| PreToolUse | session-timer, hcom-pre-tool-use | comms-schema-lint NOT registered here (only PostToolUse) |
| PostToolUse | comms-schema-lint | Acts only on `Edit\|Write\|MultiEdit` to comms ledger files |
| SessionStart | session-timer | |
| SessionEnd | session-cleanup, hcom-session-end | |
| Stop | stop.sh | |
| SubagentStop | subagent-stop.sh | |
| PreCompact | pre-compact.sh | |

The directive's hypothesis that comms-schema-lint also fires on PreToolUse is incorrect — settings.json registers it only under `PostToolUse`. This eliminates one of the audited race-pair candidates.

### Dependency analysis (sibling hooks on same event)

| Pair | Shared state | Race? | Severity |
|------|--------------|-------|----------|
| session-timer ↔ hcom-pre-tool-use (PreToolUse) | `~/.claude/session-timers/<sid>.agent` — session-timer's `00-parse` may CREATE it (via process walk + write); hcom-pre-tool-use READS it | **YES (silent benign)** | LOW (described below) |
| session-cleanup ↔ hcom-session-end (SessionEnd) | none direct (timer files vs broker DB rows). Both may read `<sid>.agent` to resolve agent name; only session-cleanup deletes it. | partial — hcom-session-end may read `.agent` AFTER session-cleanup deletes it → falls through to env var `CLAUDE_AGENT_NAME` or no-op | LOW (degradation only — locks may not be released this session; reaper or TTL cleanup catches it) |
| stop.sh ↔ subagent-stop.sh (different events) | `_spawns.log` shared (subagent-stop appends EXIT row; stop.sh tails the log) | partial — stop.sh may snapshot before subagent-stop's EXIT row lands | LOW (snapshot may miss last EXIT, recoverable from log itself) |

### `<sid>.agent` race detail (PreToolUse)

`session-timer.sh` sources `modules/00-parse.sh`. On the first PreToolUse where `$AGENT_FILE` is missing, `00-parse` walks the process tree, computes `AGENT_NAME`, and writes it to `~/.claude/session-timers/<sid>.agent`.

`hcom-pre-tool-use.sh` (line 27) reads the same file:
```bash
agent_file="$HOME/.claude/session-timers/${session_id}.agent"
[ -f "$agent_file" ] && agent_name=$(cat "$agent_file" 2>/dev/null)
```

If hcom-pre-tool-use runs BEFORE session-timer's first call has produced the file, hcom-pre-tool-use's `[ -f ... ]` check fails and the hook short-circuits (`[ -z "$agent_name" ] && exit 0` at line 30). Effect: ZERO HCOM messages injected on the very first PreToolUse of a fresh session. From the second PreToolUse onward the file exists (subsequent racing reads see a fully-written file because writes are atomic at this size). Net cost: at most one missed message-injection cycle per session, recovered automatically on the next tool call.

This is a harmless silent miss, not a corrupted state. Acceptable.

### `<sid>.agent` race detail (SessionEnd)

`session-cleanup.sh` (line 39) calls `rm_session_files "$SESSION_ID"`, which removes `<sid>.agent`. `hcom-session-end.sh` (line 21) reads `<sid>.agent` to resolve agent name. If hcom-session-end races after session-cleanup, the read fails and the hook exits without releasing locks (line 23: `[ -z "$agent_name" ] && exit 0`).

Mitigations already in place:
- `CLAUDE_AGENT_NAME` env var has higher precedence than the file (line 18) — when present, race is moot.
- `hcom-session-end.sh` line 29 also runs an unconditional `DELETE FROM file_locks WHERE acquired_at + ttl_sec < strftime('%s','now')` — TTL-expired locks are cleaned regardless. Locks held by this specific agent that haven't yet expired remain until TTL or until the next session-end / reaper run.

Severity: LOW. Locks self-heal via TTL; lifecycle is correct in steady state.

### `_spawns.log` race detail (Stop ↔ SubagentStop)

Different events fire on different agent boundaries: SubagentStop fires when a worker exits; Stop fires when the parent session stops. If the parent session ends in the same instant a final worker is exiting, `stop.sh`'s `tail -100 "$spawn_log"` may capture the file before the worker's EXIT row is appended.

Recovery: the master `_spawns.log` always retains the EXIT row (subagent-stop's append is independent). Only the per-session snapshot under `_stop-snapshots/` may miss the row, and audits should always cross-check the master log anyway.

Severity: LOW. Documented snapshot caveat; no fix needed.

### Internal modules/ ordering (sequential, in-process)

`session-timer.sh` dispatches modules sequentially via a single source loop (lines 44-50, then explicit `run_mod` calls at 60-70). So modules ARE ordered:

```
mod_parse  →  mod_gc  →  mod_bootstrap_check  →  mod_context_check  →
mod_nudge  →  mod_baseline_stash  →  mod_spawn_log  →  mod_counter  →
mod_commit_gate  →  mod_notebook_guard  →  mod_timer
```

**Critical contract**: `00-parse.sh::mod_parse` SETS the globals `SESSION_ID`, `AGENT_NAME`, `TOOL_NAME`, `START_FILE`, `OVERRIDE_FILE`, `AGENT_FILE`, `PID_FILE`, `CLAUDE_PID`. Every subsequent module depends on these. This is in-process sequential dispatch — single bash process, one source loop, deterministic ordering. SAFE.

The numeric prefix `NN-name.sh` IS load-bearing here, but only because `LC_COLLATE=C` + glob expansion (`[0-9]*.sh`) deterministically orders the source loop. This is enforced by the dispatcher, not by Claude Code's hook scheduler.

If we ever migrate `15-baseline-stash` or `45-spawn-log` to standalone hook scripts (registered directly in settings.json), they'd lose the parse stage's globals and break. Migration would require each module to re-implement its own JSON parsing + agent walk.

## Recommendations

### Immediate action items
**NONE URGENT** — current setup is race-safe in the only way that matters: races are bounded, silent, and self-healing. No corruption, no data loss.

### Documentation
- This audit codifies the modules/ contract: `00-parse` provides shared globals; downstream modules depend on those globals; in-process sequential dispatch makes that safe. Document in any future module additions.
- Settings.json hook registration is the source of truth for what runs when. The directive's claim that comms-schema-lint runs on PreToolUse was wrong — verify settings.json before writing audit text.

### Future-proofing rules
1. **DO NOT** convert any `modules/NN-*.sh` entry into a standalone settings.json hook unless it sets its own globals. Doing so breaks the parse-dependent contract.
2. **Before adding a new sibling hook to any event**, audit for read-after-write against the CURRENT hook set on that event. Specifically:
   - Does the new hook read a file written by an existing sibling?
   - Does the new hook write a file read by an existing sibling?
   - If yes to either, choose ONE of (a)/(b)/(c) below.
3. **If 2 sibling hooks ever NEED ordering** (rare):
   - (a) Wrap both in a single dispatcher script (like session-timer wraps modules)
   - (b) Use file-based coordination (flag files; bash flock)
   - (c) Move logic into modules/ (gains in-process sequential dispatch)
4. **HCOM-specific note**: `hcom-pre-tool-use.sh` already self-protects (silent no-op when broker DB or agent file missing). Future HCOM hooks should follow the same defensive pattern — assume the file/DB may not exist yet, exit gracefully.

## Cross-References
- H5 source: github.com/anthropics/claude-code/issues/21533 (closed not-planned)
- Settings.json: `~/.claude/settings.json` § hooks
- Dispatcher: `~/.claude/hooks/session-timer.sh`
- Modules: `~/.claude/hooks/modules/00-parse.sh` through `50-bootstrap.sh`
- Sibling hooks (PreToolUse): `~/.claude/hooks/hcom-pre-tool-use.sh`
- Sibling hooks (SessionEnd): `~/.claude/hooks/session-cleanup.sh`, `~/.claude/hooks/hcom-session-end.sh`
- Lock TTL cleanup: `~/.claude/hooks/hcom-session-end.sh` line 29
