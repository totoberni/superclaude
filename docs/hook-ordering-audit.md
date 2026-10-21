# Hook Ordering Audit

The original audit (2026-05-09) analysed ordering and output races between the parallel bash hooks of the previous system. That system is gone; this stub records why the class of problem no longer exists.

## Current design

- Each hook event runs exactly ONE process: `~/.claude/hooks/dispatch.py <Event>`, registered by `bin/apply-settings.ps1 -Profile superclaude` (run by the owner).
- The dispatcher reads stdin once, imports the handler modules under `~/.claude/hooks/sc_hooks/handlers/` in sorted module-name order, and calls every handler registered with `@on(event, tools, name)` for that event and tool, in registration order.
- Handlers return a `Result`; they never write to stdout. The dispatcher aggregates all results into a single response: all block reasons together (exit 2, stderr) or all notes joined into one `additionalContext` JSON object. Multiple JSON outputs and interleaved writes cannot occur.
- Order does not affect the outcome: every matching handler runs even when an earlier one blocks, and a block from any handler wins. Handlers must therefore not depend on another handler's side effects within the same event.
- A handler exception is fail-open: it is logged to `~/.claude/logs/hooks.log` and the remaining handlers still run.
- Kill switches: `SUPERCLAUDE_GUARDS=off`, `SUPERCLAUDE_GUARD_<NAME>=off|warn|block`.

## Residual ordering concern

Hooks merge across settings levels (user, project, local). A project-level hook would run as a separate process alongside the dispatcher; keep superclaude logic inside handlers rather than adding further hook commands.

Guards present: write-acl, content-scan, git-policy, commit-gate, git-verb, heuristics, worker-verify, notebook-guard, key-guard, wait. Design detail: `~/.claude/docs/guard-subsystem.md`.
