# Guard Subsystem

The mechanical enforcement tier on native Windows 11. It replaces the former bash hook system.

## Design

- **One dispatcher per event**: `settings.json` registers a single command per hook event, `python ~/.claude/hooks/dispatch.py <Event>` (base interpreter, standard library only, to stay fast). Events wired: SessionStart, SubagentStart, PreToolUse, PostToolUse, SubagentStop, PreCompact, Stop, SessionEnd.
- **Handlers**: modules under `~/.claude/hooks/sc_hooks/handlers/`, each a function `handle(ctx) -> Result | None` registered with `@on(event, tools, name)` (empty `tools` = every tool). A handler never prints and never exits; it returns `Result.deny(reason)` or `Result.note(text)`.
- **Single aggregated response**: `sc_hooks/core.py` runs every matching handler and writes ONE response. Any block on PreToolUse yields exit code 2 with the reasons on stderr (`GUARD-BLOCK [name] ...`); otherwise the notes are joined into one `additionalContext` JSON object. There are no parallel hook processes, so no ordering races.
- **Fail-open**: a handler that raises (or fails to import) is skipped and the traceback is appended to `~/.claude/logs/hooks.log`. A bug in a guard never blocks the session.
- **Identity**: role (`meta` / `worker` / `plain`) comes from the payload's `agent_type` for subagents, else from `CLAUDE_AGENT_NAME` (set by the `meta` launcher).
- **Paths**: `core.norm_path` canonicalises `C:\x`, `C:/x`, `/c/x`, `~/x` to one lowercase form before any comparison (NTFS is case-insensitive).

## Guards present

write-acl, content-scan, git-policy, commit-gate, git-verb, heuristics, notebook-guard (PreToolUse), and worker-verify (PostToolUse on Agent, advisory).

Non-guard handlers in the same package: memory-inject, context-check, thinking-nudge, baseline-stash, spawn-log, snapshots, session-cleanup, latex-warn.

There is no sandbox on native Windows: enforcement is the write-ACL guard plus `permissions.deny`.

Activation and kill switches: `~/.claude/docs/guard-activation.md`.
