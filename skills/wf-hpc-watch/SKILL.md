---
name: wf-hpc-watch
description: "Use when monitoring a long-running SLURM job on a fixed poll interval, acting only when its state changes. Meta+orch only."
category: workflow
user-invocable: true
argument-hint: "<job-id|job-name> [--interval 15m]"
allowed-tools: Read, Bash
---

# wf-hpc-watch

Scheduled poll-then-act binding for a long-running SLURM job. On a fixed interval the loop reads the job's live queue state and tails its output, classifies the run as RUNNING, DONE, or FAILED, and acts ONLY when that classification changes since the previous tick. On a terminal success it stops and reports the final metric; on a terminal failure it hands off to a fix workflow or escalates. wf-hpc-watch configures the watch and PRINTS a ready-to-paste `/loop` + `/goal` pair; the external engine enforces the exit. The skill never arms the engine itself.

**Authority**: meta + orch only. The armed loop dispatches a fix worker or an output reviewer when the job's state changes, and a `w-*` worker cannot spawn those, so the loop conductor must be meta or orch. The one-shot setup invocation (reading the job context and printing the block) is strictly read-only.

## What this binds

wf-hpc-watch is a thin binding of `/converge` on engine binding **B3** (poll-then-act monitor). It fixes the generic converge slots to SLURM job supervision:

- **External signal**: one SLURM job (by id or name). Its live state is read each tick via a read-only status query (`squeue`, `sacct`) or by parsing already-synced `slurm-<id>.out` / `.err` through the `/hpc` `status` parser. The job is the producer; its terminal SLURM state is the producer-completion signal (the analogue of a `STATUS: DONE`).
- **Poll (per tick)**: a cheap read of the queue state plus a short tail of the output. No subagent runs on an unchanged state.
- **Act (on transition only)**: on RUNNING to DONE, report the final metric and, if an output audit is configured, dispatch a FRESH reviewer via `/review-dispatch`. On RUNNING to FAILED, hand the error tail to a fix workflow (a `w-debugger` over `slurm-<id>.err`) or raise `ESCALATE`.

Loop mechanics (round ledger, two-token protocol, caps, post-compaction requote) are inherited from `/converge`; this file states only the poll-specific loop body and the terminal-state predicate.

## Loop body (per tick)

Each tick is one cheap poll followed by an act-only-on-change decision. Five steps in order:

1. **POLL**: read the job's live state with a read-only query (`sacct -j <id> --format=JobID,State,Elapsed,ExitCode,MaxRSS -P`, or `squeue -j <id>` while it is PENDING or RUNNING). This mutates nothing on the cluster.
2. **TAIL**: read the last ~40 lines of `slurm-<id>.out` and `.err` (via the `/hpc` `status` parser) for progress, the final metric, or an error signature. Local and cheap.
3. **CLASSIFY**: map the SLURM state to RUNNING (PENDING, RUNNING, SUSPENDED, COMPLETING), DONE (COMPLETED), or FAILED (FAILED, TIMEOUT, CANCELLED, OUT_OF_MEMORY, NODE_FAIL, PREEMPTED, BOOT_FAIL). Compare against the last-known classification in the tick state file `~/.claude/.wf-hpc-watch.<job-id>.state` (under `~/.claude/`, never the project or a cluster-synced tree).
4. **ACT ONLY ON TRANSITION**: if the classification is unchanged (still RUNNING), record the tick timestamp and STOP this tick silently; run no subagent. On RUNNING to DONE, report the final metric and (if configured) dispatch a fresh reviewer over the output artefact. On RUNNING to FAILED, quote the error tail and dispatch the fix workflow or raise `ESCALATE`.
5. **PERSIST**: write the new classification and tick timestamp to the state file and append a ledger entry (tick, observed SLURM state, action taken) before any fix subagent runs. Load-bearing state lives on disk, never only in the tick message.

## Goal predicate

The loop converges when the job reaches a TERMINAL state, read FRESH on the most recent tick:

- **DONE (success gate)**: the most recent poll reported `COMPLETED`. The conductor reports the final metric; if an output audit is configured, a FRESH reviewer over the job's output artefact returns a clean `SEAL: ACCEPTED` (`blocking=0 major=0 minor=0`; `nits=0` at the gate/strict bar).
- **FAILED (escalate)**: any FAILED-class terminal state routes to the fix workflow (a `w-debugger` over the error tail) or, if unrecoverable, `ESCALATE`. A silent stop on failure is forbidden.

A terminal state observed from a CACHED earlier tick never fires the goal; the reading must be fresh on the tick that terminates the loop (no pre-approval). If the job vanishes from the queue with no terminal state seen within the cap (walltime plus one interval), the conductor declares `ESCALATE` (the poll went blind).

## Emitted /loop + /goal block

Setup ENDS by printing a ready-to-paste `/loop` + `/goal` pair, then STOPS (DEC-R2: the external judge stays independent; wf-hpc-watch NEVER arms `/loop` or `/goal` itself). The human pastes both to arm the engine. Template (fill `<job-id>` and the `--interval` value):

```
/loop 15m Run one wf-hpc-watch poll tick for SLURM job <job-id>. POLL its live state read-only (sacct -j <job-id> --format=JobID,State,Elapsed,ExitCode,MaxRSS -P, or squeue -j <job-id>); TAIL the last 40 lines of slurm-<job-id>.out and .err; CLASSIFY as RUNNING, DONE (COMPLETED), or FAILED (FAILED/TIMEOUT/CANCELLED/OUT_OF_MEMORY/NODE_FAIL/PREEMPTED/BOOT_FAIL); compare with the last-known state in ~/.claude/.wf-hpc-watch.<job-id>.state. ACT ONLY ON TRANSITION: if still RUNNING, record the timestamp and stop silently; on RUNNING to DONE report the final metric and (if configured) dispatch a fresh reviewer over the output; on RUNNING to FAILED quote the error tail and dispatch a w-debugger over slurm-<job-id>.err or raise ESCALATE. NEVER submit, cancel, hold, or resubmit the job; the poll is read-only.
```
```
/goal Accept only when ALL hold: (1) the conductor states that the MOST RECENT poll tick read SLURM job <job-id> in a TERMINAL state (COMPLETED, FAILED, TIMEOUT, CANCELLED, OUT_OF_MEMORY, NODE_FAIL, PREEMPTED, or BOOT_FAIL) FRESH this tick, not from a cached earlier tick; (2) on COMPLETED the conductor reported the final metric and, if an output audit is configured, quoted a line beginning "SEAL: ACCEPTED" from a FRESH reviewer over the job output, most recent and post-dating the run, reporting blocking=0 major=0 minor=0 (nits=0 at gate/strict); on any FAILED-class terminal state the conductor dispatched the fix workflow or raised ESCALATE rather than stopping silently. If poll ticks exceed the cap (job walltime plus one interval) with no terminal state observed, declare ESCALATE and stop.
```

**Durability**: `/loop` schedules die with the session, and an orch session hard-blocks at 53 minutes (rule 25). A SLURM job running hours to days outlives any single interactive or orch session, so a bare `/loop 15m` cannot monitor it end to end; run the schedule headless (`claude -p`) or on the toto layer to outlive the session, or accept that monitoring resumes only when a session is live.

Print the pair, then stop. The human pastes `/loop` and `/goal` to arm the engine.

## Constraints

- **NEVER** submit, cancel, hold, release, or resubmit a job from the loop; the poll issues only read-only status queries (`squeue`, `sacct`) or parses synced output files. Resubmission on preemption is a human or conductor decision, never an autonomous loop action.
- **NEVER** issue a state-mutating remote command (`sbatch`, `srun`, `scancel`, `scontrol`) from a tick; a monitor observes, it does not steer the cluster.
- **NEVER** run the expensive act (reviewer or fix worker) on an unchanged state; act only on a classification transition. The 15m default interval suits an expensive remote poll and respects login-node etiquette; do not poll faster than the scheduler updates.
- **NEVER** arm `/loop` or `/goal` yourself; print the block and stop (DEC-R2).
- **NEVER** author a `VERDICT` or `SEAL` token as the loop conductor; only reviewer subagents emit them, quoted verbatim.
- **NEVER** invoke wf-hpc-watch from a `w-*` worker; the transition act needs spawn authority, so only meta and orch drive the loop.
- Keep the tick state file and ledger under `~/.claude/`, never inside the project or a cluster-synced tree (superclaude firewall).

## Cross-References

- Loop engine + binding B3: `/converge` (Engine bindings)
- SLURM status parsing (read-only, never submits): `/hpc` (`status` subcommand)
- Reviewer resolution (output audit): `/review-dispatch`
- Fix workflow on FAILED: `w-debugger` (via `/delegate` or `/autocommission`)
- Verdict tokens + severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Campaign plan: `~/.claude/plans/wf-skills/plan.md`
