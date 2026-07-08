---
name: wf-nb-watch
description: "Use when watching a long notebook run, dispatching a fix on BROKEN or HUNG."
category: workflow
user-invocable: true
argument-hint: "<notebook-path> [--interval 10m]"
allowed-tools: Read, Bash
---

# wf-nb-watch

Scheduled poll-then-act binding for a long-running notebook execution. On a fixed interval the loop reads `nb-monitor`'s progress file and per-cell status, classifies the run as RUNNING, DONE, BROKEN, or HUNG, and acts ONLY when that classification changes since the previous tick. On a clean completion it seals the executed notebook; on BROKEN or HUNG it dispatches a fix round and re-runs, or escalates. wf-nb-watch configures the watch and PRINTS a ready-to-paste `/loop` + `/goal` pair; the external engine enforces the exit. The skill never arms the engine itself.

**Authority**: meta + orch only. The armed loop dispatches a fix worker and a seal reviewer on a state change, and a `w-*` worker cannot spawn those, so the loop conductor must be meta or orch. The one-shot setup invocation (reading the notebook path and printing the block) is strictly read-only.

## What this binds

wf-nb-watch is a thin binding of `/converge` on engine binding **B3** (poll-then-act monitor). It fixes the generic converge slots to notebook-run supervision:

- **External signal**: one long-running notebook executed under `/nb-monitor`, which is the SINGLE writer of the progress file `~/.claude/.nb-progress.json` (schema 1). The notebook run is the producer; a clean completion (`status="done"`, exit 0) is the producer-completion signal.
- **Poll (per tick)**: a cheap local read of the progress file (`current_index`/`total_cells`, `cell_elapsed_s`, `last_output_line`, `status`, `kernel`, `updated`). No subagent runs on an unchanged state.
- **Act (on transition only)**: on RUNNING to DONE, dispatch a FRESH reviewer via `/review-dispatch` to seal the executed notebook. On RUNNING to BROKEN or HUNG, dispatch a fix round (a `w-debugger` scoped to the failing cell, the fix applied through `/notebook` `nb batch`) and re-run under `/nb-monitor`, or raise `ESCALATE`.

Loop mechanics (round ledger, two-token protocol, caps, post-compaction requote) are inherited from `/converge`; this file states only the poll-specific loop body and the completion predicate.

## Loop body (per tick)

Each tick is one cheap read followed by an act-only-on-change decision. Five steps in order:

1. **POLL**: read `~/.claude/.nb-progress.json` (atomic single-writer file; a reader never sees a half-written snapshot). Record `status`, `current_index`/`total_cells`, `updated`, and `kernel`.
2. **INSPECT**: read `last_output_line` (the tqdm relay) and `cell_elapsed_s` for the live cell to distinguish genuine progress from a stall.
3. **CLASSIFY**: map to RUNNING (`status="running"` with a fresh `updated`), DONE (`status="done"`, exit 0), BROKEN (`status="broken"`, a cell raised), or HUNG (`status="hung"`: cell timeout exceeded or `kernel="dead"`). A SLOW-but-alive cell (stdout advancing, `updated` fresh) is RUNNING, NOT HUNG. Compare against the last-known classification in the tick state file `~/.claude/.wf-nb-watch.<stem>.state`.
4. **ACT ONLY ON TRANSITION**: if the classification is unchanged (still RUNNING), record the tick timestamp and STOP this tick silently; run no subagent. On RUNNING to DONE, dispatch a fresh reviewer to seal the executed notebook. On RUNNING to BROKEN or HUNG, quote the `error` summary and dispatch a fix round, then re-run under `/nb-monitor`, or raise `ESCALATE`.
5. **PERSIST**: write the new classification and tick timestamp to the state file and append a ledger entry (tick, `status`, cell index, action taken) before any fix subagent runs. Load-bearing state lives on disk, never only in the tick message.

## Goal predicate

The loop converges only on a DUAL stop criterion; BOTH must hold on the same tick:

- **(a) CLEAN COMPLETION**: the most recent poll read `status="done"` (exit 0) with NO cell in BROKEN or HUNG. This is the producer-completion signal, read fresh this tick.
- **(b) CLEAN SEAL**: a FRESH reviewer over the executed notebook returns `SEAL: ACCEPTED` (`blocking=0 major=0 minor=0`; `nits=0` at the gate/strict bar), the most recent such line, post-dating the last cell re-execution.

A completion read from a CACHED earlier tick never fires the goal (no pre-approval); any re-run after a fix voids a prior SEAL and requires a fresh one. If BROKEN or HUNG persists after the cap (default 4 fix-and-rerun rounds), or total findings do not decrease across 2 consecutive rounds, the conductor declares `ESCALATE` and stops.

## Emitted /loop + /goal block

Setup ENDS by printing a ready-to-paste `/loop` + `/goal` pair, then STOPS (DEC-R2: the external judge stays independent; wf-nb-watch NEVER arms `/loop` or `/goal` itself). The human pastes both to arm the engine. Template (fill `<notebook-path>` and the `--interval` value):

```
/loop 10m Run one wf-nb-watch poll tick for <notebook-path>. POLL ~/.claude/.nb-progress.json (single-writer, atomic); INSPECT last_output_line and cell_elapsed_s; CLASSIFY as RUNNING (status running, updated fresh), DONE (status done, exit 0), BROKEN (status broken), or HUNG (status hung or kernel dead); a SLOW-but-alive cell with advancing stdout is RUNNING not HUNG. Compare with the last-known state in ~/.claude/.wf-nb-watch.<stem>.state. ACT ONLY ON TRANSITION: if still RUNNING, record the timestamp and stop silently; on RUNNING to DONE dispatch a fresh reviewer to seal the executed notebook; on RUNNING to BROKEN or HUNG quote the error summary, dispatch a w-debugger scoped to the failing cell (fix applied through /notebook nb batch), re-run under /nb-monitor, or raise ESCALATE. NEVER edit the .ipynb directly and NEVER launch a second nb-monitor on the same notebook.
```
```
/goal Accept only when ALL hold: (1) the conductor states that the MOST RECENT poll tick read nb-monitor status="done" (exit 0) with no cell BROKEN or HUNG, FRESH this tick and not from a cached earlier tick; (2) the transcript contains a line beginning "SEAL: ACCEPTED" quoted verbatim from a FRESH reviewer over the executed notebook, the MOST RECENT such line, post-dating the last cell re-execution, reporting blocking=0 major=0 minor=0 (nits=0 at gate/strict). If BROKEN or HUNG persists after 4 fix-and-rerun rounds, or total findings do not decrease across 2 consecutive rounds, declare ESCALATE and stop.
```

**Durability**: `/loop` schedules die with the session, and an orch session hard-blocks at 53 minutes (rule 25). A long notebook run can exceed a single interactive or orch session, so a bare `/loop 10m` cannot watch it end to end; run the schedule headless (`claude -p`) or on the toto layer to outlive the session, or accept that monitoring resumes only when a session is live.

Print the pair, then stop. The human pastes `/loop` and `/goal` to arm the engine.

## Constraints

- **NEVER** blindly re-execute the notebook each tick; the loop READS the progress file (a cheap local `cat`) and re-runs ONLY after a fix lands on a BROKEN or HUNG cell (act on transition). The 10m default interval suits a long run.
- **NEVER** edit the `.ipynb` directly; every fix routes through `/notebook` (`nb batch`) and every re-run through `/nb-monitor`. The `30-notebook-guard.sh` PreToolUse hook hard-blocks direct `.ipynb` edits regardless.
- **NEVER** launch a second concurrent `nb-monitor` on the same notebook; it is the single writer of the progress file, and two runs race the kernel even though the file write itself is atomic.
- **NEVER** treat a SLOW-but-alive cell as HUNG; only a `status="hung"`, a dead kernel, or a stale `updated` past the cell timeout is HUNG. A false HUNG classification triggers a needless fix round.
- **NEVER** arm `/loop` or `/goal` yourself; print the block and stop (DEC-R2).
- **NEVER** author a `VERDICT` or `SEAL` token as the loop conductor; only reviewer subagents emit them, quoted verbatim.
- **NEVER** invoke wf-nb-watch from a `w-*` worker; the transition act needs spawn authority, so only meta and orch drive the loop.
- Keep the tick state file and ledger under `~/.claude/`, never inside the project tree (superclaude firewall; notebook skill state also lives under `~/.claude/`).

## Cross-References

- Loop engine + binding B3: `/converge` (Engine bindings)
- Notebook run monitor (progress file, BROKEN/HUNG/SLOW states): `/nb-monitor`
- Notebook mutation + execution (atomic, kernel-aware): `/notebook`
- Reviewer resolution (executed-notebook audit): `/review-dispatch`
- Fix round on BROKEN/HUNG: `w-debugger` (via `/delegate` or `/autocommission`)
- Verdict tokens + severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Campaign plan: `~/.claude/plans/wf-skills/plan.md`
