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
3. Must work with our existing hook pipeline (session-timer.sh runs first)
4. Must be configurable per agent tier (meta may need different rules than workers)

### Integration Plan (when ready)

1. Install parry binary
2. Create `~/.claude/hooks/parry-scan.sh` wrapper
3. Register as PreToolUse + PostToolUse in settings.json
4. Test with `test-hooks.sh` extended suite
5. Monitor false positive rate for 1 week before enforcing blocks
