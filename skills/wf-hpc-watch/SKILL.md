---
name: wf-hpc-watch
description: "Use when polling a long-running SLURM job, acting only on state change."
category: workflow
user-invocable: true
argument-hint: "<job-id|job-name> [--interval 15m]"
allowed-tools: Read, Bash
---

# wf-hpc-watch

Scheduled poll-then-act monitor for a long-running SLURM job. On a fixed interval the monitor reads the job's live queue state and tails its output, classifies the run as RUNNING, DONE, or FAILED, and acts ONLY when that classification changes since the previous tick. On a terminal success it stops and reports the final metric; on a terminal failure it passes the error tail to a fix workflow or escalates to the owner. wf-hpc-watch configures the watch and PRINTS a ready-to-paste `/loop` + `/goal` pair; the external engine enforces the exit. The skill never arms the engine itself.

**Authority**: meta or the owner's plain session. The armed monitor dispatches a fix worker or an output reviewer when the job's state changes, and a `w-*` worker cannot spawn those. The one-shot setup invocation (reading the job context and printing the block) is strictly read-only.

## What this monitors

- **External signal**: one SLURM job (by id or name). Its live state is read each tick via a read-only status query over ssh (`ssh <alias> squeue ...`, `ssh <alias> sacct ...`, alias from `~/.ssh/config`; Windows ships an OpenSSH client) or by parsing already-synced `slurm-<id>.out` / `.err` through the `/hpc` `status` parser. The job is the producer; its terminal SLURM state is the producer-completion signal (the analogue of a `STATUS: DONE`).
- **Poll (per tick)**: a cheap read of the queue state plus a short tail of the output. No subagent runs on an unchanged state.
- **Act (on transition only)**: on RUNNING to DONE, report the final metric and, if an output audit is configured, dispatch a FRESH one-shot reviewer (`w-reviewer` or `w-hostile-reviewer`) over the output artefact. On RUNNING to FAILED, pass the error tail to a fix workflow (a `w-debugger` over `slurm-<id>.err`) or raise `ESCALATE`.

## Tick body

Each tick is one cheap poll followed by an act-only-on-change decision. Five steps in order:

1. **POLL**: read the job's live state with a read-only query (`sacct -j <id> --format=JobID,State,Elapsed,ExitCode,MaxRSS -P`, or `squeue -j <id>` while it is PENDING or RUNNING), run on the cluster through `ssh <alias>`. This mutates nothing on the cluster.
2. **TAIL**: read the last ~40 lines of `slurm-<id>.out` and `.err` (via the `/hpc` `status` parser) for progress, the final metric, or an error signature. Local and cheap.
3. **CLASSIFY**: map the SLURM state to RUNNING (PENDING, RUNNING, SUSPENDED, COMPLETING), DONE (COMPLETED), or FAILED (FAILED, TIMEOUT, CANCELLED, OUT_OF_MEMORY, NODE_FAIL, PREEMPTED, BOOT_FAIL). Compare against the last-known classification in the tick state file `~/.claude/.wf-hpc-watch.<job-id>.state` (under `~/.claude/`, never the project or a cluster-synced tree).
4. **ACT ONLY ON TRANSITION**: if the classification is unchanged (still RUNNING), record the tick timestamp and STOP this tick silently; run no subagent. On RUNNING to DONE, report the final metric and (if configured) dispatch a fresh reviewer over the output artefact. On RUNNING to FAILED, quote the error tail and dispatch the fix workflow or raise `ESCALATE`.
5. **PERSIST**: write the new classification and tick timestamp to the state file (Write tool) and append a log entry (tick, observed SLURM state, action taken) before any fix subagent runs. Load-bearing state lives on disk, never only in the tick message.

## Stop condition

The monitor stops when the job reaches a TERMINAL state, read FRESH on the most recent tick:

- **DONE (success)**: the most recent poll reported `COMPLETED`. The conductor reports the final metric; if an output audit is configured, a FRESH reviewer over the job's output artefact returns `VERDICT: CLEAN` (`blocking=0 major=0`). A `VERDICT: REWORK` is reported to the owner with its findings; the monitor does not iterate on it.
- **FAILED (escalate)**: any FAILED-class terminal state routes to the fix workflow (a `w-debugger` over the error tail) or, if unrecoverable, `ESCALATE`. A silent stop on failure is forbidden.

A terminal state observed from a CACHED earlier tick never stops the monitor; the reading must be fresh on the tick that terminates it (no pre-approval). If the job vanishes from the queue with no terminal state seen within the cap (walltime plus one interval), the conductor declares `ESCALATE` (the poll went blind).

## Emitted /loop + /goal block

Setup ENDS by printing a ready-to-paste `/loop` + `/goal` pair, then STOPS (the external judge stays independent; wf-hpc-watch NEVER arms `/loop` or `/goal` itself). The human pastes both to arm the engine. Template (fill `<job-id>`, `<alias>` and the `--interval` value):

```
/loop 15m Run one wf-hpc-watch poll tick for SLURM job <job-id>. POLL its live state read-only over ssh <alias> (sacct -j <job-id> --format=JobID,State,Elapsed,ExitCode,MaxRSS -P, or squeue -j <job-id>); TAIL the last 40 lines of slurm-<job-id>.out and .err; CLASSIFY as RUNNING, DONE (COMPLETED), or FAILED (FAILED/TIMEOUT/CANCELLED/OUT_OF_MEMORY/NODE_FAIL/PREEMPTED/BOOT_FAIL); compare with the last-known state in ~/.claude/.wf-hpc-watch.<job-id>.state. ACT ONLY ON TRANSITION: if still RUNNING, record the timestamp and stop silently; on RUNNING to DONE report the final metric and (if configured) dispatch a fresh reviewer over the output; on RUNNING to FAILED quote the error tail and dispatch a w-debugger over slurm-<job-id>.err or raise ESCALATE. NEVER submit, cancel, hold, or resubmit the job; the poll is read-only.
```
```
/goal Accept only when ALL hold: (1) the conductor states that the MOST RECENT poll tick read SLURM job <job-id> in a TERMINAL state (COMPLETED, FAILED, TIMEOUT, CANCELLED, OUT_OF_MEMORY, NODE_FAIL, PREEMPTED, or BOOT_FAIL) FRESH this tick, not from a cached earlier tick; (2) on COMPLETED the conductor reported the final metric and, if an output audit is configured, quoted verbatim the "VERDICT:" line of a FRESH reviewer over the job output, post-dating the run; on any FAILED-class terminal state the conductor dispatched the fix workflow or raised ESCALATE rather than stopping silently. If poll ticks exceed the cap (job walltime plus one interval) with no terminal state observed, declare ESCALATE and stop.
```

**Durability**: `/loop` schedules die with the session. A SLURM job running hours to days outlives any single interactive session, so a bare `/loop 15m` cannot monitor it end to end. Either register a per-user Windows Task Scheduler task that runs the tick headless (`claude -p "<the /loop tick body above>"`) on the chosen interval, or simply re-invoke `/wf-hpc-watch <job-id>` when a session is live: the state file makes every tick resumable, and monitoring resumes from the last recorded classification.

Print the pair, then stop. The human pastes `/loop` and `/goal` to arm the engine.

## Constraints

- **NEVER** submit, cancel, hold, release, or resubmit a job from the monitor; the poll issues only read-only status queries (`squeue`, `sacct`) or parses synced output files. Resubmission on preemption is a human or conductor decision, never an autonomous action.
- **NEVER** issue a state-mutating remote command (`sbatch`, `srun`, `scancel`, `scontrol`) from a tick; a monitor observes, it does not steer the cluster.
- **NEVER** run the expensive act (reviewer or fix worker) on an unchanged state; act only on a classification transition. The 15m default interval suits an expensive remote poll and respects login-node etiquette; do not poll faster than the scheduler updates.
- **NEVER** arm `/loop` or `/goal` yourself; print the block and stop.
- **NEVER** author a `VERDICT` token as the conductor; only reviewer subagents emit it, quoted verbatim.
- **NEVER** invoke wf-hpc-watch from a `w-*` worker; the transition act needs spawn authority.
- Keep the tick state file and log under `~/.claude/`, never inside the project or a cluster-synced tree (superclaude firewall).

## Cross-References

- SLURM status parsing (read-only, never submits): `/hpc` (`status` subcommand)
- Output audit: `w-reviewer` or `w-hostile-reviewer`, one-shot (`/review`, `/hostile-review`)
- Fix workflow on FAILED: `w-debugger` (via `/autocommission`)
- Verdict tokens + severity map: `~/.claude/skills/_shared/verdict-schema.md`
