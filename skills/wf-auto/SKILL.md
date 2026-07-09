---
name: wf-auto
description: "Use when configuring an unattended converge loop the owner then launches"
category: workflow
user-invocable: true
argument-hint: "<target> --class <artifact-class> [--bar default|gate|strict] [--rounds N] [--budget-usd B]"
allowed-tools: Read, Write, Bash
---

# wf-auto

Configures the supervised-autonomous convergence driver (`converge_auto.py`): a phased headless `claude -p` process that iterates produce, review, and seal rounds on its own, enforcing every no-pre-approval guarantee mechanically instead of through a human pasting `/goal` each round. wf-auto validates a loop config, materializes the reviewer resolution from `/review-dispatch` into it, writes the runtime files, and PRINTS the ready-to-run launch command. It NEVER launches the driver itself (DEC-A1); the owner's one consent act is running the printed command.

**Authority**: meta + orch only, the same spawn-adjacent boundary as `/converge`. This skill spawns nothing; its `allowed-tools` are Read, Write, Bash, no Agent or Skill, because launching happens outside the skill entirely.

## Positioning

Interactive `/converge` stays exactly as sealed: the conductor drives rounds in-session and prints a `/goal` block the human pastes each time the loop needs arming (DEC-R2). wf-auto is the AUTONOMOUS variant, for trusted or overnight work: instead of N per-round pastes, the owner performs ONE consent act, launching the driver process, and no human touches the loop again until it seals or escalates. wf-auto itself never arms `/goal` or `/loop`, and never runs the driver in-process; it only prepares the config the driver reads.

## What the skill does

1. **Gather and validate**: read the target, `--class`, `--bar`, `--rounds`, and `--budget-usd` arguments; resolve `repo`, `artifact_paths`, and `task_spec_file` from the target; default `rounds_cap` to 4 and `phase_timeout_s` to 2400s unless overridden.
2. **Materialize reviewer resolution**: look up `<artifact-class>` in the `/review-dispatch` resolution table (reviewer agent, model, rubric path) and write those values into `reviewer_agent`, `reviewer_model`, and `rubric_path` in `loop.json`. The driver carries zero review policy; review-dispatch stays the sole policy source of truth, this step just freezes its output into the config.
3. **Create the runtime dir**: `<campaign plans dir>/auto/<loop-id>/`, holding `loop.json`, the task spec file, and the scaffolding the driver populates as it runs (`handoff.json`, `rounds.md`, `prompts/`, `raw/`).
4. **Write loop.json**: the full config schema below, plus the task spec file the round-1 producer objective is built from.
5. **Dry-run validate**: run `~/.claude/.venv/bin/python ~/.claude/scripts/swarm/converge_auto.py --config <runtime-dir>/loop.json --dry-run` to prove the config parses and the referenced paths, agents, and rubric exist, at zero API spend.
6. **Print and stop**: print the launch command block below and STOP. wf-auto never executes it.

## Config schema (loop.json)

```json
{
  "loop_id": "<slug>",
  "repo": "<abs path>",
  "artifact_paths": ["<path>", "..."],
  "task_spec_file": "<abs path>",
  "artifact_class": "<code-small|code-large|frontend|methodology|infra|figures|test-integrity>",
  "producer_agent": "w-implementer",
  "producer_model": null,
  "reviewer_agent": "<materialized from /review-dispatch>",
  "reviewer_model": "<materialized>",
  "rubric_path": "<materialized>",
  "seal_agent": "w-hostile-reviewer",
  "bar": "default|gate|strict",
  "rounds_cap": 4,
  "phase_budget_usd": 5.0,
  "phase_timeout_s": 2400,
  "test_cmd": null,
  "notify_cmd": null
}
```

`producer_agent`/`seal_agent` default as shown; override only when the target artifact needs a different producer or a non-hostile seal agent the owner names explicitly. Optional keys (all nullable): `allowed_tools` (default `["Bash"]`), `producer_effort`, `reviewer_effort`, `seal_model`, `seal_effort`, and `allow_dirty` (default false: the driver REFUSES to start if the artifact scope carries uncommitted changes, keeping diff attribution clean; set true only deliberately).

## Mechanical guarantees (DEC-A2, enforced by the driver's code, never by this skill's prose)

wf-auto documents these six guards so the owner knows what launching buys; it implements none of them, `converge_auto.py` does:

1. **Fresh reviewer per round**: every review is a NEW `claude -p` session; the driver builds the review prompt from artifact + diff + rubric ONLY, so isolation is prompt construction, not policy text.
2. **Producer token ban**: the driver scans every producer return for `VERDICT:`/`SEAL:` line patterns; a match fails the round as a protocol violation.
3. **Seal-revision binding**: in a git repo the driver commits the artifact scope as a pre-seal snapshot (`chore(<loop_id>): round K pre-seal snapshot` plus the co-author trailer) and binds the seal to that real commit hash (`commit:<12hex>`), re-verifying after the seal returns that HEAD is unchanged AND the scope is clean; for non-git artifacts it binds to the content manifest (`sha256:<12hex>`) and re-verifies that instead. Any mismatch voids the seal and forces a fresh seal round.
4. **Stale-seal rejection**: only the most recent seal that post-dates the last artifact change can terminate the loop, enforced by the revision check (commit hash in git mode, content manifest otherwise) plus a monotonic round counter in the handoff file.
5. **Seal-auditor identity**: the seal phase always runs a brand-new session under `--no-session-persistence`, making a resume of any round-reviewer session structurally impossible, not just forbidden by instruction.
6. **Caps**: a round cap (default 4) and a non-decreasing-findings clause (findings flat or rising across 2 consecutive rounds) force ESCALATE rather than a further round.

## Emitted launch command

Setup ends by printing this block, then STOPS (DEC-A1: the skill never runs it):

```
nohup ~/.claude/.venv/bin/python ~/.claude/scripts/swarm/converge_auto.py \
  --config <runtime-dir>/loop.json \
  >> <runtime-dir>/driver.log 2>&1 &
```

Tail `driver.log` for phase-by-phase progress and `<runtime-dir>/rounds.md` for the round ledger. Exit codes: `0` sealed, `2` escalated (check `notify_cmd` or the ledger's last `escalate:` row), `3` config or validation error (the loop never started), `4` environment error (the `claude` CLI is absent or auth is dead, checked before any phase runs).

## Durability and observability

The driver is a detached background process (`nohup ... &`); it outlives the session that launched it and survives the owner closing the terminal. `rounds.md` uses the structured row schema pre-committed in the campaign design (`## R<round>.<phase> - <ts> - <event>`, w1-design.md), which wf-watchdog can read as a heartbeat and the upcoming swarm-observe parses deterministically, so an unattended run stays supervisable without touching the driver. When `notify_cmd` is set in loop.json, an ESCALATE fires it with the escalation reason; otherwise the owner discovers an escalation by reading `rounds.md` or `driver.log`.

For git-repo loops an optional post-commit hardening hook makes seal-voiding live in git itself, independent of the driver's lifetime: `~/.claude/.venv/bin/python ~/.claude/scripts/swarm/converge_auto.py --install-void-hook <repo>` idempotently installs `scripts/swarm/seal-void-hook.sh` as the repo's `post-commit` hook (chaining, never clobbering, any pre-existing hook). After a loop seals, any later commit that changes the sealed artifact scope appends a `seal_voided_post_hoc` escalate row to that loop's `rounds.md` and drops a `VOIDED` marker in its runtime dir. Voiding is content-precise: the driver records the sealed CONTENT manifest and the hook voids only when a recomputed manifest of the artifact scope differs from it, so a byte-identical re-commit (a squash, an amend, a message rewrite) is safe while any real content change voids. The hook is fail-safe (it always exits 0 and needs `jq`) and is never installed silently; the owner or wf-auto setup runs the install verb deliberately.

### /commit false composition

When the target repo is under a `/commit false` policy the driver composes with that policy instead of fighting it. It detects the policy exactly as `hooks/modules/15-baseline-stash.sh` does (that hook is the pattern source of truth): the `CLAUDE_COMMIT_POLICY=false` environment variable wins, otherwise the repo basename appears in `~/.claude/hooks/no-commit-projects.local` (overridable for the driver via `CONVERGE_NO_COMMIT_FILE`; blank and `#comment` lines ignored). In that mode the driver makes no `git add` or `git commit` call, binds the seal to the content manifest (`sha256:<12hex>`) exactly as it does for non-git artifacts, feeds the reviewer and seal auditor a diff computed from the round-0 snapshot rather than `git diff` against HEAD (so attribution stays clean on a chronically dirty tree), and skips the dirty-start refusal. The `driver.log` and the round-1 ledger delta record `policy=/commit-false`. The content-precise void hook composes here too: because it voids on a manifest change rather than a mere path-touch, the owner committing an already-sealed `/commit false` tree with unchanged sealed bytes does not void the seal.

## Constraints

- **NEVER** launch the driver; validate with `--dry-run`, print the launch command, and stop (DEC-A1).
- **NEVER** author a `VERDICT` or `SEAL` token; those come only from the driver's spawned reviewer and seal sessions.
- **NEVER** weaken a guard flag (`--no-session-persistence`, `--disallowedTools Write,Edit,NotebookEdit`, `--permission-mode acceptEdits`) in the printed launch command; it must match what the driver's guards require verbatim.
- **NEVER** point `artifact_paths` outside the intended write scope; the producer's write allowlist is exactly this list.
- **NEVER** invoke this skill from a `w-*` worker; only meta and orch hold spawn-adjacent authority for autonomous loops.
- **NEVER** leave `phase_budget_usd`, `phase_timeout_s`, or `rounds_cap` unset or infinite; every guard needs a finite bound to escalate against.

## Cross-References

- Loop mechanics, phase graph, guard placement (frozen spec, SOT for this skill): `~/.claude/plans/wf-autonomy/checkpoints/w1-design.md`
- Driver script: `~/.claude/scripts/swarm/converge_auto.py`
- Interactive counterpart (print-then-paste `/goal`): `/converge`
- Reviewer resolution table (policy SOT, materialized into loop.json): `/review-dispatch`
- Token protocol: `~/.claude/skills/_shared/verdict-schema.md`
- Dispatch contract: `~/.claude/skills/_shared/dispatch-contract.md`
- No pre-approval: `~/.claude/rules/40-swarm-quality-gates.md` R-5
- Campaign plan: `~/.claude/plans/wf-autonomy/plan.md`
