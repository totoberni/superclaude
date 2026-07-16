# Guard Activation Lifecycle

The mechanical guard subsystem ships INERT: the guard code under `hooks/guards/` does nothing
until `settings.json` wires the two dispatchers. This doc is how to turn that wiring on and off
safely.

## Two-layer model

| Layer | What | State |
|---|---|---|
| 1: guard code | `hooks/guard-dispatch.sh`, `hooks/guard-post.sh`, `hooks/guards/*.sh` | Committed, version-controlled, inert on its own (sourcing defines functions only, no top-level side effects) |
| 2: wiring | `settings.json` entries under `hooks.PreToolUse` (guard-dispatch.sh) and `hooks.PostToolUse` (guard-post.sh) | Machine-local, gitignored, not part of the repo or any push |

Because `settings.json` is gitignored, wiring is per-machine: pushing or pulling the guard code
never activates or deactivates it anywhere. Each machine's guard state is set independently.

## Activating

Run the owner-only apply script:

```bash
~/projects/apply-superclaude-guards.sh
```

It is idempotent (safe to re-run, never adds duplicate hook entries), auto-backs-up
`settings.json` to a timestamped `settings.json.bak-guards-<timestamp>` file before touching it,
and wires both dispatchers with a 10 second hook timeout. It also restores that backup and aborts
if the resulting JSON fails validation.

Hooks are read at session start, so **restart the session/app** after running the script; the new
`PreToolUse`/`PostToolUse` entries do not take effect in an already-running session.

## Kill-switch and fail-open

- Set `SUPERCLAUDE_GUARDS=off` to disable every guard at once without unwiring `settings.json`
  (`guard_kill_switch` in `hooks/guards/lib-guard.sh` checks this env var first, before any
  per-guard logic runs).
- Both dispatchers are fail-open: any internal fault (missing lib, bad JSON, absent `jq`) prints a
  `GUARD-WARN` and passes the tool call through; only an explicit `guard_block` in block mode
  exits 2. A broken guard never bricks tool use.

## Deactivating

Either restore a `settings.json.bak-guards-*` backup (`cp <backup> ~/.claude/settings.json`), or
manually remove the two hook entries, then restart the session. For a temporary, reversible
disable that needs no restart, use the kill-switch above instead.

## The /git policy flag

Distinct from wiring: the `26-git-policy` guard reads `~/.claude/config/git-policy`, written only
by the `/git` skill. `/git false` blocks commit/push; `/git true` allows; an absent file is
treated as enabled (fail-open, matching the rest of this subsystem). See
`skills/git/SKILL.md` for the full scope and blocked/unblocked vectors; not restated here.
