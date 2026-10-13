# Guard Activation Lifecycle

How the guards described in `~/.claude/docs/guard-subsystem.md` are switched on, tuned, and switched off.

## Registration

The dispatcher is registered into `~/.claude/settings.json` by `bin/apply-settings.ps1 -Profile superclaude`, run by the owner (agents do not edit `settings.json`). The script writes one `dispatch.py <Event>` command per hook event; until it has been run, no guard is active. New handlers need no further registration: any module dropped into `~/.claude/hooks/sc_hooks/handlers/` is imported by the dispatcher on the next event.

## Modes and kill switches

Each guard runs in one of three modes, default `block`:

| Mode | Effect |
|---|---|
| `block` | A deny on PreToolUse stops the tool call (exit 2, reason returned to the model) |
| `warn` | The deny is downgraded to a `GUARD-WARN [name]` note; the tool call proceeds |
| `off` | The handler is not run |

Environment variables (set in the shell before launch, or in the `env` block of `settings.json`):

- `SUPERCLAUDE_GUARDS=off` disables every handler.
- `SUPERCLAUDE_GUARD_<NAME>=off|warn|block` sets one guard. `<NAME>` is the guard name upper-cased with non-alphanumerics as underscores, for example `SUPERCLAUDE_GUARD_WRITE_ACL=warn`, `SUPERCLAUDE_GUARD_GIT_POLICY=off`.

Guards present: write-acl, content-scan, git-policy, commit-gate, git-verb, heuristics, worker-verify, notebook-guard.

## Rollout and diagnosis

Introduce a new guard in `warn`, watch the notes, then promote it to `block`. A deny outside PreToolUse is always advisory. If a guard seems silent, read `~/.claude/logs/hooks.log`: handler exceptions are fail-open and only show up there.
