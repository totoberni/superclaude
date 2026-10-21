---
name: wf-nb-watch
description: "Use when watching a long notebook run, dispatching a fix on BROKEN or HUNG."
category: workflow
user-invocable: true
argument-hint: "<notebook-path> [--interval 10m]"
allowed-tools: Read, Bash
---

# wf-nb-watch

Scheduled poll-then-act monitor for a long-running notebook execution. On a fixed interval the monitor reads `nb-monitor`'s progress file and per-cell status, classifies the run as RUNNING, DONE, BROKEN, or HUNG, and acts ONLY when that classification changes since the previous tick. On a clean completion it dispatches a one-shot review of the executed notebook; on BROKEN or HUNG it dispatches a fix round and re-runs, or escalates. wf-nb-watch configures the watch and PRINTS a ready-to-paste `/loop` + `/goal` pair; the external engine enforces the exit. The skill never arms the engine itself.

**Authority**: meta or the owner's plain session. The armed monitor dispatches a fix worker and a reviewer on a state change, and a `w-*` worker cannot spawn those. The one-shot setup invocation (reading the notebook path and printing the block) is strictly read-only.

## What this monitors

- **External signal**: one long-running notebook executed under `/nb-monitor`, which is the SINGLE writer of the progress file `~/.claude/.nb-progress.json` (schema 1). The notebook run is the producer; a clean completion (`status="done"`, exit 0) is the producer-completion signal.
- **Poll (per tick)**: a cheap local read of the progress file (`current_index`/`total_cells`, `cell_elapsed_s`, `last_output_line`, `status`, `kernel`, `updated`). No subagent runs on an unchanged state.
- **Act (on transition only)**: on RUNNING to DONE, dispatch a FRESH one-shot reviewer (`w-reviewer` or `w-hostile-reviewer`) over the executed notebook. On RUNNING to BROKEN or HUNG, dispatch a fix round (a `w-debugger` scoped to the failing cell, the fix applied through `/notebook` `nb batch`) and re-run under `/nb-monitor`, or raise `ESCALATE`.

## Tick body

Each tick is one cheap read followed by an act-only-on-change decision. Five steps in order:

1. **POLL**: read `~/.claude/.nb-progress.json` with the Read tool (atomic single-writer file; a reader never sees a half-written snapshot). Record `status`, `current_index`/`total_cells`, `updated`, and `kernel`.
2. **INSPECT**: read `last_output_line` (the tqdm relay) and `cell_elapsed_s` for the live cell to distinguish genuine progress from a stall.
3. **CLASSIFY**: map to RUNNING (`status="running"` with a fresh `updated`), DONE (`status="done"`, exit 0), BROKEN (`status="broken"`, a cell raised), or HUNG (`status="hung"`: cell timeout exceeded or `kernel="dead"`). A SLOW-but-alive cell (stdout advancing, `updated` fresh) is RUNNING, NOT HUNG. Compare against the last-known classification in the tick state file `~/.claude/.wf-nb-watch.<stem>.state`.
4. **ACT ONLY ON TRANSITION**: if the classification is unchanged (still RUNNING), record the tick timestamp and STOP this tick silently; run no subagent. On RUNNING to DONE, dispatch a fresh reviewer over the executed notebook. On RUNNING to BROKEN or HUNG, quote the `error` summary and dispatch a fix round, then re-run under `/nb-monitor`, or raise `ESCALATE`.
5. **PERSIST**: write the new classification and tick timestamp to the state file (Write tool) and append a log entry (tick, `status`, cell index, action taken) before any fix subagent runs. Load-bearing state lives on disk, never only in the tick message.

## Stop condition

The monitor stops only on a DUAL criterion; BOTH must hold on the same tick:

- **(a) CLEAN COMPLETION**: the most recent poll read `status="done"` (exit 0) with NO cell in BROKEN or HUNG. This is the producer-completion signal, read fresh this tick.
- **(b) REVIEW VERDICT**: a FRESH reviewer over the executed notebook has returned its `VERDICT:` line, post-dating the last cell re-execution. `VERDICT: CLEAN` (`blocking=0 major=0`) closes the watch; `VERDICT: REWORK` is reported to the owner with its findings, who decides whether a fix and re-review follow.

A completion read from a CACHED earlier tick never stops the monitor (no pre-approval); any re-run after a fix voids a prior verdict and requires a fresh one. If BROKEN or HUNG persists after the cap (default 4 fix-and-rerun attempts), the conductor declares `ESCALATE` and stops.

## Emitted /loop + /goal block

Setup ENDS by printing a ready-to-paste `/loop` + `/goal` pair, then STOPS (the external judge stays independent; wf-nb-watch NEVER arms `/loop` or `/goal` itself). The human pastes both to arm the engine. Template (fill `<notebook-path>` and the `--interval` value):

```
/loop 10m Run one wf-nb-watch poll tick for <notebook-path>. POLL ~/.claude/.nb-progress.json (single-writer, atomic); INSPECT last_output_line and cell_elapsed_s; CLASSIFY as RUNNING (status running, updated fresh), DONE (status done, exit 0), BROKEN (status broken), or HUNG (status hung or kernel dead); a SLOW-but-alive cell with advancing stdout is RUNNING not HUNG. Compare with the last-known state in ~/.claude/.wf-nb-watch.<stem>.state. ACT ONLY ON TRANSITION: if still RUNNING, record the timestamp and stop silently; on RUNNING to DONE dispatch a fresh one-shot reviewer over the executed notebook; on RUNNING to BROKEN or HUNG quote the error summary, dispatch a w-debugger scoped to the failing cell (fix applied through /notebook nb batch), re-run under /nb-monitor, or raise ESCALATE. NEVER edit the .ipynb directly and NEVER launch a second nb-monitor on the same notebook.
```
```
/goal Accept only when ALL hold: (1) the conductor states that the MOST RECENT poll tick read nb-monitor status="done" (exit 0) with no cell BROKEN or HUNG, FRESH this tick and not from a cached earlier tick; (2) the transcript contains a line beginning "VERDICT:" quoted verbatim from a FRESH reviewer over the executed notebook, the MOST RECENT such line, post-dating the last cell re-execution. If BROKEN or HUNG persists after 4 fix-and-rerun attempts, declare ESCALATE and stop.
```

**Durability**: `/loop` schedules die with the session. A long notebook run can exceed a single interactive session, so a bare `/loop 10m` cannot watch it end to end. Either register a per-user Windows Task Scheduler task that runs the tick headless (`claude -p "<the /loop tick body above>"`) on the chosen interval, or simply re-invoke `/wf-nb-watch <notebook-path>` when a session is live: the state file makes every tick resumable, and monitoring resumes from the last recorded classification.

Print the pair, then stop. The human pastes `/loop` and `/goal` to arm the engine.

## Constraints

- **NEVER** blindly re-execute the notebook each tick; the monitor READS the progress file (a cheap local read) and re-runs ONLY after a fix lands on a BROKEN or HUNG cell (act on transition). The 10m default interval suits a long run.
- **NEVER** edit the `.ipynb` directly; every fix routes through `/notebook` (`nb batch`) and every re-run through `/nb-monitor`. The notebook guard hard-blocks direct `.ipynb` edits regardless.
- **NEVER** launch a second concurrent `nb-monitor` on the same notebook; it is the single writer of the progress file, and two runs race the kernel even though the file write itself is atomic.
- **NEVER** treat a SLOW-but-alive cell as HUNG; only a `status="hung"`, a dead kernel, or a stale `updated` past the cell timeout is HUNG. A false HUNG classification triggers a needless fix round.
- **NEVER** arm `/loop` or `/goal` yourself; print the block and stop.
- **NEVER** author a `VERDICT` token as the conductor; only reviewer subagents emit it, quoted verbatim.
- **NEVER** invoke wf-nb-watch from a `w-*` worker; the transition act needs spawn authority.
- Keep the tick state file and log under `~/.claude/`, never inside the project tree (superclaude firewall; notebook skill state also lives under `~/.claude/`).

## Cross-References

- Notebook run monitor (progress file, BROKEN/HUNG/SLOW states): `/nb-monitor`; to launch the run detached from the session, `scpy job.py start --name <id> -- <nb-monitor command line>` (the scheduled polling here is unchanged)
- Notebook mutation + execution (atomic, kernel-aware): `/notebook`
- Executed-notebook audit: `w-reviewer` or `w-hostile-reviewer`, one-shot (`/review`, `/hostile-review`)
- Fix round on BROKEN/HUNG: `w-debugger` (via `/autocommission`)
- Verdict tokens + severity map: `~/.claude/skills/_shared/verdict-schema.md`
