---
name: wf-hygiene
description: "Use when a scheduled advisory hygiene pass is needed: session status, memory health, stale checkpoints. B4 gardener, Meta+orch only."
category: workflow
user-invocable: true
argument-hint: "[--interval daily]"
allowed-tools: Read, Bash
---

# wf-hygiene

Low-frequency gardener loop that runs advisory hygiene checks on a schedule: session lifecycle status, memory DB health, and checkpoint staleness. Each tick produces a short report; the skill itself never deletes, kills, or mutates anything. wf-hygiene binds `/converge`'s **B4** row (gardener / maintenance): there is no artefact under iterative production and review, no round cap, and no SEAL. The loop runs until the human stops it.

**Authority**: meta + orch only. wf-hygiene never spawns a producer or a reviewer, so it needs neither `Agent` nor `Skill`; its only dependencies are `Read` (inspect plan and checkpoint files) and `Bash` (run the canonical status scripts directly). The conductor (meta or orch) is whoever re-arms the `/loop` tick after tick; workers hold no standing schedule.

## What this binds

wf-hygiene binds `/converge`'s **B4 gardener / maintenance** row: ongoing upkeep with no fixed endpoint, driven by a dynamic bare `/loop` (self-paced) or a fixed daily interval, steerable live via a hot-editable `~/.claude/loop.md` where one is set up. It fixes the generic B4 slot to three read-only checks:

- **Session status**: zombie or stopped processes, meta-session cap breaches, timer-file health, via the same canonical runner `/session-reaper` uses: `~/.claude/scripts/session-status.sh`.
- **Memory DB health**: the `/mem-health --quick` score (right-sized rows, FTS cohesion, vec soundness, metadata completeness), skipping the near-dup scan to keep each tick cheap.
- **Checkpoint staleness**: files under any `~/.claude/plans/*/checkpoints/*.md` older than a configurable threshold (suggested default: 7 days) whose parent campaign's `plan.md` still reads `Status: ACTIVE`, a signal that a session ended without picking the checkpoint back up.

Unlike wf-design, wf-websearch, and wf-report, no artefact is being iterated toward a clean seal here, so wf-hygiene does not inherit converge's produce/review/SEAL machinery. It borrows only the `/loop` cadence and the reporting discipline.

## Loop body (per tick)

Each tick is a read-only scan, never a produce/review round. Four steps in order:

1. **SESSION CHECK**: run `~/.claude/scripts/session-status.sh` (the script `/session-reaper status` itself uses); note zombie count, meta-session cap state, and any timer file whose PID is dead.
2. **MEMORY CHECK**: run `bash ~/.claude/scripts/mem-health.sh --quick`; capture the `SCORE: NN/100` line and any fired v3 trigger.
3. **CHECKPOINT SWEEP**: glob `~/.claude/plans/*/checkpoints/*.md` (e.g. `find ~/.claude/plans/*/checkpoints -name '*.md' -mtime +7`), then cross-check each stale hit's parent `plan.md` status line; only flag checkpoints belonging to a still-ACTIVE campaign.
4. **SUMMARISE AND FLAG**: fold the three signals into one short report. Anything actionable (a zombie to kill, a DB score below threshold, an orphaned checkpoint) is listed as a flagged item FOR THE HUMAN; wf-hygiene never acts on its own findings.

## Goal predicate

B4 gardener has **no goal predicate**, unlike B1 (`wf-design`, `wf-websearch`, `wf-report`) or the B3 poll-then-act bindings. There is no artefact being iterated to a seal, no producer, no reviewer, and therefore no `SEAL: ACCEPTED` line ever fires for this loop. wf-hygiene runs until the human stops it, whether that means killing the `/loop`, removing a cron entry, or editing `loop.md` to point elsewhere. The only exit condition is a human decision, not an engine-evaluated predicate: do not paste a `/goal` block for this skill, there is nothing to seal.

## Emitted /loop block

Setup ENDS by printing a ready-to-paste `/loop` block, then STOPS (DEC-R2, generalised to `/loop`: the human arms the schedule, wf-hygiene NEVER self-arms). The block INLINES the tick body into the `/loop` prompt (like the sibling monitors), so each tick runs the checks directly; it does not re-invoke `/wf-hygiene` (which would only reprint this block and stop). Two shapes, depending on `--interval`:

Bare (dynamic; the model self-paces the tick cadence):
```
/loop Run the wf-hygiene tick: bash ~/.claude/scripts/session-status.sh; bash ~/.claude/scripts/mem-health.sh --quick; find ~/.claude/plans -name plan.md -exec grep -l "Status: ACTIVE" {} + then sweep their checkpoints older than 7 days; summarise findings and flag anything needing human action; act on nothing (advisory only).
```

Fixed daily (a 24h tick needs a durable runner; a bare interactive session will not survive to fire it, see the durability caveat):
```
/loop 24h Run the wf-hygiene tick: bash ~/.claude/scripts/session-status.sh; bash ~/.claude/scripts/mem-health.sh --quick; sweep checkpoints older than 7 days under ACTIVE plans; summarise and flag; act on nothing (advisory only).
```

A user-level `~/.claude/loop.md`, where one is set up, may hold either inline body as the default so a bare `/loop` (no arguments) resolves to the hygiene tick without retyping it each session.

**Durability**: `/loop` schedules die with the session, and a 24h cadence cannot fire inside a single interactive or orch session (orchs hard-block at 53 min). A daily gardener needs a durable runner (OS cron invoking headless `claude -p`, or the toto scheduling layer), not a bare interactive `/loop`. Print the block, then stop; the human arms the schedule.

## Constraints

- **NEVER** delete, kill, or mutate anything directly; wf-hygiene only reads status and reports. Any destructive action (killing a zombie session, pruning memory rows, deleting a stale checkpoint) stays a human decision, executed via `/session-reaper kill`, `/lt-mem`, or a manual `rm`, never by this loop.
- **NEVER** pass `--all` or any destructive flag to the session-reaper runner from within the loop body; only the status path is called.
- **NEVER** treat a fired `mem-health` v3 trigger as authorisation to run `/lt-mem --compact` automatically; surface it as a flagged item only.
- **NEVER** self-arm `/loop`; print the block and stop (DEC-R2).
- **NEVER** invent a SEAL or a goal predicate for this binding; B4 has none (see Goal predicate).
- Honours DEC-R3's destructive-tier gates: `session-reaper kill` keeps its own in-body unattended-context gate; wf-hygiene never pre-confirms on the human's behalf.

## Cross-References

- Loop engine + B4 binding definition: `/converge` (Engine bindings B1-B5)
- Session status and zombie check: `/session-reaper`
- Memory DB health score: `/mem-health`
- Verdict/goal-string conventions (why B1 has a SEAL and B4 does not): `~/.claude/skills/_shared/verdict-schema.md`
- Campaign plan: `~/.claude/plans/wf-skills/plan.md`
