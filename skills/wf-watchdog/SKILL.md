---
name: wf-watchdog
description: "Use when supervising another converge loop's health: read its round ledger as a heartbeat and escalate on stall or oscillation."
category: workflow
user-invocable: true
argument-hint: "<ledger-path> [--interval 10m]"
allowed-tools: Read, Bash
---

# wf-watchdog

Supervisory loop over ANOTHER convergence loop. It composes the time engine (`/loop`, a short recurring tick) with the convergence engine (`/goal`, a turn-level stop): every interval it reads the watched loop's round ledger (`rounds.md`) as a heartbeat and classifies its health; on stall or oscillation it escalates. wf-watchdog CONFIGURES the loop and PRINTS a ready-to-paste `/loop` + `/goal` block, then stops. It never arms either engine itself (DEC-R2); the human pastes the block.

**Authority**: meta + orch (the conductor supervising the watched loop). This skill only READS a ledger and prints blocks; it spawns nothing and never edits the watched loop, so its `allowed-tools` are Read + Bash.

## What this binds

wf-watchdog is a thin binding of `/converge` on binding **B5** (watchdog over another loop). It fixes the generic B5 slots to a ledger heartbeat:

- **Signal**: the watched loop's round ledger at `<ledger-path>` (a `/converge` `rounds.md`: timestamp, round number, delta, the quoted VERDICT line, open-findings count). The watchdog READS it; it never writes the ledger or the watched loop.
- **Cadence**: a short fixed `/loop` tick (default 10m via `--interval`), deliberately faster than the watched loop's round cadence so a stall is caught within a tick or two.
- **Heartbeat**: the latest ledger entry's timestamp is the liveness beat; its open-findings trend is the progress beat. A frozen timestamp means stall; a non-decreasing findings count means oscillation.

Loop mechanics (tick order, post-compaction requote) are inherited from `/converge`; this file states only the heartbeat loop body and the stop predicate.

## Loop body (per tick)

Each tick is one ledger read followed by a health verdict. Four steps in order:

1. **READ** (read-only): read the latest entry of `<ledger-path>`: its timestamp, round number, quoted VERDICT line, and open-findings count. Record them against the previous tick's snapshot.
2. **STALL check**: if no new round entry has appeared in N consecutive ticks (timestamp and round number unchanged), classify STALL. N defaults to the number of watchdog ticks spanning one expected round of the watched loop.
3. **OSCILLATION check**: if the open-findings count is flat or rising across 2+ successive rounds (findings not monotonically decreasing), classify OSCILLATION. This mirrors the `/converge` non-decreasing-findings escape clause.
4. **CLASSIFY + ACT**: STALL or OSCILLATION escalates, surfacing an alert to the conductor (and sending an ntfy notification if an ntfy helper is configured). A latest verdict of `SEAL: ACCEPTED` is noted as the watched loop converging. Otherwise HEALTHY: log and stay silent.

## Goal predicate

The watchdog stops on EITHER outcome (a disjunctive stop, not the dual seal of a producing loop):

- **(a) WATCHED LOOP SEALED**: the ledger's latest verdict line begins `SEAL: ACCEPTED` (the watched loop converged; the conductor states this is the most recent line and that NO round entry post-dates it, no pre-approval per `verdict-schema.md`). The watchdog's job is done. It sees only the ledger, so seal-freshness is bounded by ledger fidelity.
- **(b) ESCALATE FIRED**: the watchdog raised STALL (no new entry in N ticks) or OSCILLATION (findings did not decrease across 2 consecutive rounds). The watched loop needs conductor intervention; the watchdog stops having surfaced the alert.

Unlike a producing loop, the watchdog does not seek a clean seal of its OWN; it terminates the moment the watched loop either seals or provably wedges.

## Emitted /loop + /goal block

Setup ENDS by printing BOTH ready-to-paste blocks, then STOPS (DEC-R2: the engines stay independent; wf-watchdog NEVER arms `/loop` or `/goal` itself). The human pastes them: `/loop` arms the recurring heartbeat read, `/goal` arms the stop. Template (specialise `<interval>` from `--interval`, default 10m; `<ledger-path>` from the positional arg; `N` from the watched loop's round cadence):

```
/loop <interval> Read the latest entry of the round ledger at <ledger-path> (timestamp, round, VERDICT line, open-findings count). Compare against the previous tick. STALL if no new round entry in N consecutive ticks; OSCILLATION if open findings are flat or rising across 2+ rounds; else HEALTHY. On STALL or OSCILLATION surface an alert to the conductor (ntfy if configured); on a latest SEAL: ACCEPTED note convergence; otherwise stay silent. Never edit the ledger or the watched loop.
```

```
/goal Accept when EITHER holds: (1) the round ledger at <ledger-path> shows a latest verdict line beginning "SEAL: ACCEPTED", which the conductor states is the most recent line and which NO round entry post-dates (the watched loop converged and nothing changed after the seal; the watchdog sees only the ledger, so seal-freshness is bounded by ledger fidelity); OR (2) this watchdog has fired ESCALATE because the ledger stalled (no new entry in N ticks) or oscillated (open findings did not decrease across 2 consecutive rounds). Stop on either.
```

Print both blocks, then stop. The human pastes them to arm the engines.

## Constraints

- **NEVER** arm `/loop` or `/goal` yourself; print both blocks and stop (DEC-R2).
- **NEVER** edit the watched loop or its ledger; the watchdog is strictly read-only over `<ledger-path>`.
- **NEVER** author a VERDICT or SEAL token; the watchdog READS the tokens the watched loop's reviewers wrote, it never mints them (`verdict-schema.md`, Provenance).
- **NEVER** seal on a stale ledger line; the SEAL check requires the MOST RECENT verdict line with NO round entry post-dating it (no pre-approval).
- **DURABILITY CAVEAT**: `/loop` dies with the session (not a durable cron; max 50 tasks). For a watchdog that must outlive the session, run it headless or on toto.
- **NEVER** invoke from a `w-*` worker; supervising a loop is a meta/orch conductor role.

## Cross-References

- Loop engine + binding table + round ledger schema: `/converge` (binding B5, section Round ledger)
- Verdict tokens (SEAL/VERDICT) + severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Sister poll monitor (B3, broker health): `/wf-wave-monitor`
- Campaign plan: `~/.claude/plans/wf-skills/plan.md`
