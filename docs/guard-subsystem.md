# Guard Subsystem

The mechanical enforcement tier: invariants that used to be advisory (text in
rules/skills an agent could ignore) are now enforced by hooks, each with a
bite-test. Anchors: `rules/13-worker-first-mandate.md` (worker delegation
invariants the guards mechanize) and `rules/40-swarm-quality-gates.md`
(R-1..R-5 quality gates). Do not restate those files here.

## Dispatcher model

Two dispatchers, `guard-dispatch.sh` (PreToolUse) and `guard-post.sh`
(PostToolUse), each: source `lib.sh` + `guards/lib-guard.sh`, source every
`guards/[0-9]*.sh` (defines functions only; sourcing is inert), call
`guard_init`, then run an explicit ordered list of `dispatch_guard` calls.

Three safety properties:

1. **Block-guards-first ordering.** `guard_git_policy`, `guard_write_acl`,
   `guard_content_scan`, `guard_commit_gate` run before the WARN/heuristic
   guards, so a fault in a later guard can never precede a security-critical
   BLOCK.
2. **Subshell isolation.** `dispatch_guard` runs each guard as `( run_guard "$1" )`.
   A runtime abort inside one guard (unbound variable, stray `exit N`) is
   contained to its subshell and cannot skip guards ordered after it; only an
   intentional `guard_block` (exit 2) propagates.
3. **Fail-open.** Any internal fault (missing `jq`, unparseable stdin, missing
   guard file) prints a `GUARD-WARN` to stderr and passes. Only an explicit
   block in block mode blocks. Global kill-switch: `SUPERCLAUDE_GUARDS=off`.

Exit codes: `0` allow, `2` block. PostToolUse cannot block a tool that already
ran: `guard_block` degrades to a warn when `GUARD_PHASE=post`, so a
mis-authored post-guard can never exit 2.

## The 11 guards

| File | Phase | Purpose |
|---|---|---|
| `10-content-scan.sh` | Pre | Blocks em-dash/en-dash, superclaude-firewall refs, and live (un-escaped) trigger tokens in new Write/Edit/MultiEdit content or an Agent prompt |
| `20-write-acl.sh` | Pre | Path-scoped write ACL keyed on resolved agent identity (plan.md, comms dirs, settings.json, git-policy config, project-local `.claude/`) |
| `26-git-policy.sh` | Pre | Mechanical `/git true\|false` commit/push enforcement; best-effort shell-string heuristic, not a security boundary |
| `30-commit-gate.sh` | Pre | Git commit/add discipline: conventional-subject warn, mode-only-diff block, secret-shaped-string block, bulk-add warn |
| `40-git-verb.sh` | Pre | Warns on `-C` pathspec repetition and bang-mangling risk in Bash git commands |
| `50-heuristics.sh` | Pre + Post | Flags `Grep` calls carrying `head_limit` (pre); warns once near the ~20-24-call sonnet-truncation threshold (post) |
| `60-verdict-shape.sh` | Post | Validates a reviewer's VERDICT/SEAL token against the canonical grammar |
| `62-review-dispatch.sh` | Pre | Blocks a reviewer dispatch missing a ledgered `Ledger:` path, scoped to a `/converge` context; enforces reviewer isolation |
| `64-seal-binding.sh` | Pre + Post | Blocks reusing a round reviewer as the seal auditor (pre); writes the role registry and flags seal-void via `scripts/seal-manifest.py` (post) |
| `70-wrong-tool.sh` | Pre + Post | Flags governed shapes missing their marker (research/wave) and relays repeated manual-friction via `scripts/instrument-tripwire.py` |
| `80-worker-verify.sh` | Post | Injects the R-3 worker-verification obligation onto the spawning agent's next turn after a worker returns |

## Guard contract

Each `guards/NN-name.sh` defines a `guard_<name>` (PreToolUse) and/or
`guardpost_<name>` (PostToolUse) function that reads the `guard_init`-parsed
context and blocks via `guard_block` (exit 2, or a warn under degraded mode)
or flags via `guard_warn`. The numeric prefix is cosmetic: ordering is set by
the explicit `dispatch_guard` list in each dispatcher, not glob order. Every
guard ships a bite-test in `hooks/guards/tests/test-NN-name.sh`.

`26-git-policy.sh` is explicitly a best-effort shell-string heuristic, not a
security boundary; see the file header and `skills/git/SKILL.md` for its full
scope and residual-vector list.

## Adding a guard

1. Write `guards/NN-name.sh`: define `guard_<name>` and/or `guardpost_<name>`
   only (no top-level side effects).
2. Add a `dispatch_guard guard_<name>` (and/or `guardpost_<name>`) line to the
   right dispatcher, positioned before the WARN guards if it can BLOCK.
3. Write `guards/tests/test-NN-name.sh`.
4. Run `hooks/guards/tests/run-all.sh`; it must stay green.

## Activation

The subsystem is INERT until `settings.json` wires the two dispatchers; see
`docs/guard-activation.md` for that step (not duplicated here).
