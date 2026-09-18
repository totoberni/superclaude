# Security Roadmap

> Future security enhancements for superclaude infrastructure. Items here are tracked but not yet implemented.

---

## Future: Prompt Injection Scanning

[parry](https://github.com/vaporif/parry) -- Hook-based prompt injection scanner.
Scans tool inputs/outputs for injection attacks, secrets, data exfiltration.
Status: early development, monitor for maturity.
When ready: add as PreToolUse/PostToolUse hook for all agents.

### Evaluation Criteria

Before adopting:
1. Must not add >50ms per tool call (hook budget is 500ms total)
2. Must handle missing config gracefully (exit 0, not crash)
3. Must work inside our existing hook pipeline (one Python dispatcher per event; results are aggregated, handlers are fail-open)
4. Must be configurable per agent tier (meta may need different rules than workers)

### Integration Plan (when ready)

1. Install parry at user scope (no admin rights; confirm a native Windows build exists)
2. Create a `parry_scan.py` handler under `~/.claude/hooks/sc_hooks/handlers/`, registered with `@on(...)` for PreToolUse and PostToolUse
3. No `settings.json` change is needed (the dispatcher imports new handlers); start it in `warn` mode via `SUPERCLAUDE_GUARD_PARRY_SCAN=warn`
4. Add handler tests under `~/.claude/hooks/tests/`
5. Monitor false positive rate for 1 week before enforcing blocks
