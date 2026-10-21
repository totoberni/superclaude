# Guard Subsystem

The mechanical enforcement tier on native Windows 11. It replaces the former bash hook system.

## Design

- **One dispatcher per event**: `settings.json` registers a single command per hook event, `python ~/.claude/hooks/dispatch.py <Event>` (base interpreter, standard library only, to stay fast). Events wired: SessionStart, SubagentStart, PreToolUse, PostToolUse, SubagentStop, PreCompact, Stop, SessionEnd.
- **Handlers**: modules under `~/.claude/hooks/sc_hooks/handlers/`, each a function `handle(ctx) -> Result | None` registered with `@on(event, tools, name)` (empty `tools` = every tool). A handler never prints and never exits; it returns `Result.deny(reason)` or `Result.note(text)`.
- **Single aggregated response**: `sc_hooks/core.py` runs every matching handler and writes ONE response. Any block on PreToolUse yields exit code 2 with the reasons on stderr (`GUARD-BLOCK [name] ...`); otherwise the notes are joined into one `additionalContext` JSON object. There are no parallel hook processes, so no ordering races.
- **Fail-open**: a handler that raises (or fails to import) is skipped and the traceback is appended to `~/.claude/logs/hooks.log`. A bug in a guard never blocks the session. The one exception is git-policy: it reads the `/git` flag through `lib/sc_gitflag.py` and fails CLOSED (git blocked in every profile) on any defect of that lib, logged to `hooks.log` by kind.
- **Identity**: role (`meta` / `worker` / `plain`) comes from the payload's `agent_type` for subagents, else from `CLAUDE_AGENT_NAME` (set by the `meta` launcher).
- **Paths**: `core.norm_path` canonicalises `C:\x`, `C:/x`, `/c/x`, `~/x` to one lowercase form before any comparison (NTFS is case-insensitive).

## Guards present

write-acl, content-scan, git-policy, commit-gate, git-verb, heuristics, notebook-guard, key-guard, wait (PreToolUse), and worker-verify (PostToolUse on Agent, advisory).

The **wait** guard (`GUARD-BLOCK [wait]`, switch `SUPERCLAUDE_GUARD_WAIT`) stops agents from idling on a command. It rewrites in place what can be fixed mechanically (`tail -f`, `Get-Content -Wait`, a bare `python -` with no stdin) and blocks the rest: sleep/poll loops, a single sleep of 30 s or more, `find` over a drive root or the home root, `watch`, `Wait-Process` without a timeout. Monitor is denied to every non-meta `agent_type`. Every block message names the replacement (`scpy job.py wait <id>`, a scoped path, the Glob tool). Exact rules and exemptions: the module docstring of `hooks/sc_hooks/handlers/wait_guard.py`. The worker-side contract it enforces: `skills/_shared/dispatch-contract.md` § "No waiting".

Non-guard handlers in the same package: memory-inject (realm-filtered: only rows with realm IS NULL unless `SC_MEM_REALM=personal`), context-check, thinking-nudge, baseline-stash, spawn-log, snapshots, session-cleanup, latex-warn.

There is no sandbox on native Windows: enforcement is the write-ACL guard plus `permissions.deny`.

Activation and kill switches: `~/.claude/docs/guard-activation.md`.
