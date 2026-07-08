---
name: wf-wave-monitor
description: "Use when meta polls orch health on a schedule and seals when all DONE."
category: workflow
user-invocable: true
argument-hint: "[--interval 15m] [--orchs <list|all>]"
allowed-tools: Read, Bash
---

# wf-wave-monitor

Meta-facing supervision loop. It composes the time engine (`/loop`, a recurring poll) with the convergence engine (`/goal`, a turn-level seal): every interval it polls the HCOM broker for active orch health and surfaces a status digest; the goal seals the campaign once all supervised orks report DONE with zero unanswered ESC. wf-wave-monitor CONFIGURES the loop and PRINTS a ready-to-paste `/loop` + `/goal` block, then stops. It never arms either engine itself (DEC-R2); the human pastes the block.

**Authority**: meta primarily (orch for a sub-campaign). This skill only reads the broker and prints blocks; it spawns nothing, so its `allowed-tools` are Read + Bash. The conductor who pastes the block owns the armed loop and quotes any orch tokens verbatim.

## What this binds

wf-wave-monitor is a thin binding of `/converge` on binding **B3** (poll-then-act monitor). It fixes the generic B3 slots to orch supervision:

- **Signal**: the HCOM broker (`~/.claude/comms/.broker.db`), read through `broker-queries.sh` and `/comms-query`. The poll is READ-ONLY; the loop never writes the broker.
- **Cadence**: a fixed `/loop` tick (default 15m via `--interval`), gated by the `/goal` seal. B3 fires the cheap poll every tick; the expensive act (nudge, escalate) fires only on a state change, never per tick.
- **Seal**: campaign completion, not a per-artefact review SEAL. The goal holds when every supervised ork's latest RPT reports DONE and the broker shows zero unanswered ESC.

Loop mechanics (tick order, the campaign ledger, post-compaction requote) are inherited from `/converge`; this file states only the poll-specific loop body and the seal predicate.

## Loop body (per tick)

Each tick is one broker poll followed by a conditional act. Four steps in order:

1. **POLL** (read-only): run `~/.claude/scripts/comms/broker-queries.sh latest-rpt` (optionally scoped to one ork), `~/.claude/scripts/comms/broker-queries.sh unanswered-esc`, and `/comms-query stuck-orks` (orks idle beyond the 4h threshold). Scope to `--orchs <list>` when supplied, else `all`.
2. **SUMMARISE**: fold the three query returns into one digest line per ork (latest RPT seq + status, ESC backlog, idle minutes). Append the digest to the campaign ledger with a timestamp.
3. **DIFF**: compare this tick's digest against the previous tick's ledger entry. A tick with no state change is logged and otherwise SILENT (no action, no nudge).
4. **ACT (only on state change)**: a newly stuck ork triggers `/nudge <ork>`; a new unanswered ESC is surfaced for meta to answer; an ork that flipped to DONE is noted toward the seal. Acting every tick regardless of change is the failure mode B3 exists to avoid.

## Goal predicate

The loop seals on a campaign-completion predicate; BOTH must hold in the same tick:

- **(a) ALL ORKS DONE**: every supervised ork's MOST RECENT RPT-NNN reports a DONE status (orks report via RPT, not the worker `STATUS:` token), stated per ork by the conductor so no stale RPT is trusted (no pre-approval, `verdict-schema.md`). An ork with no DONE RPT, or a newer non-DONE RPT, keeps the loop open.
- **(b) ZERO UNANSWERED ESC**: `broker-queries.sh unanswered-esc` returns no rows (every ESC carries a non-null `read_at`). An open escalation blocks the seal even when every ork is idle.

Either condition alone is insufficient: all-DONE with an open ESC means work finished but a blocker went unanswered; zero-ESC without all-DONE means no blockers but work is still in flight. Only both together seal the campaign.

## Emitted /loop + /goal block

Setup ENDS by printing BOTH ready-to-paste blocks, then STOPS (DEC-R2: the engines stay independent; wf-wave-monitor NEVER arms `/loop` or `/goal` itself). The human pastes them: `/loop` arms the recurring poll, `/goal` arms the seal. Template (specialise `<interval>` from `--interval`, default 15m; `<orchs>` from `--orchs`, default all):

```
/loop <interval> Poll orch health (read-only): run ~/.claude/scripts/comms/broker-queries.sh latest-rpt, ~/.claude/scripts/comms/broker-queries.sh unanswered-esc, and /comms-query stuck-orks, scoped to <orchs>. Summarise one line per ork (latest RPT + status, ESC backlog, idle minutes) and append to the campaign ledger. Act ONLY on a state change since the previous tick: a newly stuck ork -> /nudge it; a new unanswered ESC -> surface it for meta; else stay silent.
```

```
/goal Accept only when ALL hold: (1) every supervised ork's most recent RPT reports DONE, which the conductor states is the latest RPT per ork; (2) the broker shows zero unanswered ESC (~/.claude/scripts/comms/broker-queries.sh unanswered-esc returns no rows). If the poll runs longer than <cap> ticks (the campaign's expected wall-clock divided by the interval), or a stuck ork persists across 2+ consecutive ticks with no new RPT, declare ESCALATE and stop.
```

Print both blocks, then stop. The human pastes them to arm the engines.

## Constraints

- **NEVER** arm `/loop` or `/goal` yourself; print both blocks and stop (DEC-R2).
- **NEVER** write the broker; every poll query is a read-only SELECT (`broker-queries.sh` and `/comms-query` refuse non-SELECT verbs by design).
- **NEVER** act every tick; B3 acts only on a state change since the previous tick, else it stays silent.
- **NEVER** seal on a stale RPT; the DONE check reads each ork's MOST RECENT RPT (no pre-approval, `verdict-schema.md`).
- **DURABILITY CAVEAT**: `/loop` dies with the session (it is not a durable cron; max 50 tasks). For supervision that must outlive the session, run the poll headless or on toto, not in an interactive session.
- **NEVER** invoke from a `w-*` worker; supervising a campaign is a meta/orch conductor role.

## Cross-References

- Loop engine + binding table: `/converge` (binding B3)
- Broker query helper: `~/.claude/scripts/comms/broker-queries.sh` (latest-rpt, unanswered-esc)
- Stuck-ork + ad-hoc broker queries: `/comms-query` (stuck-orks)
- Verdict tokens + severity map (orks report via RPT-NNN, distinct from worker STATUS): `~/.claude/skills/_shared/verdict-schema.md`
- Sister watchdog (B5, ledger heartbeat): `/wf-watchdog`
- Campaign plan: `~/.claude/plans/wf-skills/plan.md`
