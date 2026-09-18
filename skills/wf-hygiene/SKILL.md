---
name: wf-hygiene
description: "Use when a scheduled hygiene pass checks memory health and checkpoints."
category: workflow
user-invocable: true
argument-hint: "[--interval daily]"
allowed-tools: Read, Bash, Glob, Grep
---

# wf-hygiene

Low-frequency gardener that runs advisory hygiene checks on a schedule: memory DB health and checkpoint staleness. Each tick produces a short report; the skill itself never deletes or mutates anything. There is no artefact under production and review, no attempt cap, and no verdict. It runs until the human stops it.

**Authority**: meta or the owner's plain session. wf-hygiene never spawns a producer or a reviewer, so it needs neither `Agent` nor `Skill`; its only dependencies are `Read`/`Glob`/`Grep` (inspect plan and checkpoint files) and a shell tool (run the canonical health script directly). Workers hold no standing schedule.

## What this checks

Ongoing upkeep with no fixed endpoint, driven by a dynamic bare `/loop` (self-paced) or a fixed daily interval, steerable live via a hot-editable `~/.claude/loop.md` where one is set up. Two read-only checks:

- **Memory DB health**: the `/mem-health --quick` score (right-sized rows, FTS cohesion, vec soundness, metadata completeness), skipping the near-dup scan to keep each tick cheap.
- **Checkpoint staleness**: files under any `~/.claude/plans/*/checkpoints/*.md` older than a configurable threshold (suggested default: 7 days) whose parent campaign's `plan.md` still reads `Status: ACTIVE`, a signal that a session ended without picking the checkpoint back up.

## Tick body

Each tick is a read-only scan. Three steps in order:

1. **MEMORY CHECK**: run `scpy memory/mem_health.py --quick`; capture the `SCORE: NN/100` line and any fired v3 trigger.
2. **CHECKPOINT SWEEP**: Glob `~/.claude/plans/*/checkpoints/*.md`, keep the files whose modification time is older than the threshold, then cross-check each stale hit's parent `plan.md` status line (Grep for `Status: ACTIVE`); only flag checkpoints belonging to a still-ACTIVE campaign.
3. **SUMMARISE AND FLAG**: fold the two signals into one short report. Anything actionable (a DB score below threshold, an orphaned checkpoint) is listed as a flagged item FOR THE HUMAN; wf-hygiene never acts on its own findings.

## Stop condition

A gardener has **no goal predicate**, unlike the poll-then-act monitors (`/wf-hpc-watch`, `/wf-nb-watch`). There is no artefact, no producer, no reviewer, and therefore no `VERDICT:` line ever fires for it. wf-hygiene runs until the human stops it, whether that means stopping the `/loop`, disabling the Task Scheduler task, or editing `loop.md` to point elsewhere. The only exit condition is a human decision, not an engine-evaluated predicate: do not paste a `/goal` block for this skill.

## Emitted /loop block

Setup ENDS by printing a ready-to-paste `/loop` block, then STOPS (the human arms the schedule, wf-hygiene NEVER self-arms). The block INLINES the tick body into the `/loop` prompt (like the sibling monitors), so each tick runs the checks directly; it does not re-invoke `/wf-hygiene` (which would only reprint this block and stop). Two shapes, depending on `--interval`:

Bare (dynamic; the model self-paces the tick cadence):
```
/loop Run the wf-hygiene tick: scpy memory/mem_health.py --quick; Grep ~/.claude/plans/*/plan.md for "Status: ACTIVE", then list their checkpoints/*.md older than 7 days; summarise findings and flag anything needing human action; act on nothing (advisory only).
```

Fixed daily (a 24h tick needs a durable runner; a bare interactive session will not survive to fire it, see the durability note):
```
/loop 24h Run the wf-hygiene tick: scpy memory/mem_health.py --quick; sweep checkpoints older than 7 days under ACTIVE plans; summarise and flag; act on nothing (advisory only).
```

A user-level `~/.claude/loop.md`, where one is set up, may hold either inline body as the default so a bare `/loop` (no arguments) resolves to the hygiene tick without retyping it each session.

**Durability**: `/loop` schedules die with the session, and a 24h cadence cannot fire inside a single interactive session. A daily gardener needs a durable runner: a per-user Windows Task Scheduler task running headless `claude -p "<tick body>"` once a day. Without one, simply re-invoke the tick body by hand when a session is live. Print the block, then stop; the human arms the schedule.

## Constraints

- **NEVER** delete or mutate anything directly; wf-hygiene only reads status and reports. Any destructive action (pruning memory rows, deleting a stale checkpoint) stays a human decision, executed via `/lt-mem`, `/memory-prune`, or a manual delete of the named file, never by this skill.
- **NEVER** treat a fired `mem-health` v3 trigger as authorisation to run `/lt-mem --compact` automatically; surface it as a flagged item only.
- **NEVER** self-arm `/loop`; print the block and stop.
- **NEVER** invent a verdict or a goal predicate for this skill; it has none (see Stop condition).
- wf-hygiene never pre-confirms a destructive step on the human's behalf.

## Cross-References

- Memory DB health score: `/mem-health`
- Memory compaction and pruning (human-triggered): `/lt-mem`, `/memory-prune`
- Sibling scheduled monitors: `/wf-hpc-watch`, `/wf-nb-watch`
