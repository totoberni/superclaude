---
name: swarm-observe
description: "Use when viewing live health of converge loops, ledgers and workers"
category: delegation
user-invocable: true
argument-hint: "[--json] [--stall-min N]"
allowed-tools: Read, Bash
---

# Swarm Observe

Portfolio-level live view over convergence loops and worker activity. It reads driver
loop ledgers, hand-authored round ledgers, the spawn log and the ephemeral agent dir,
then classifies each loop's health in one aligned table. STRICTLY read-only: it never
writes, never mutates a watched loop, never kills a process.

Runs `~/.claude/scripts/swarm/swarm-observe.sh` (a deterministic bash parser). This skill
owns LOOP HEALTH; `/swarm-status` stays the sub-second in-flight process snapshot. Use
this one to answer "are my converge loops making progress, or has one wedged?".

**Mode**: $ARGUMENTS (default: full text tables)

## What it shows

| Source | What | Note |
|--------|------|------|
| `plans/*/auto/*/` (rounds.md + handoff.json) | DRIVER loops: round.phase, health class, findings trend, bound revision, age | Structured R-1 ledger grammar, parsed deterministically |
| `plans/*/rounds.md` (not under auto/) | HAND ledgers: last VERDICT/SEAL token, file-mtime age | Tolerant grep; no structured trend |
| `comms/_spawns-rich.log` | LIVE workers: SPAWN in last 24h with no same-agent_id EXIT | In-flight count + per-type |
| `agents/_ephemeral/*.md` | Ephemeral autocommissioned workers currently registered | Names only |

`_outcomes.log` is DELIBERATELY excluded. For background and agent-team dispatches its
TRUNCATED classification is captured from a JSON metadata blob rather than the worker's
final text, so it misclassifies. The `_spawns-rich.log` EXIT-row outcome (derived from
`last_assistant_message`) is the trustworthy signal instead.

Worker liveness caveat: background / team SPAWN rows carry an EMPTY agent_id, which cannot
be join-matched to an EXIT. Such rows are surfaced separately as `indeterminate (no
agent_id)`, never guessed into the in-flight count. Only rows with a real agent_id give a
reliable in-flight number.

## Classification rules

Per driver loop, first match wins (mechanizes the `/wf-watchdog` B5 stall/oscillation
doctrine: frozen timestamp = stall, non-decreasing open findings = oscillation, latest
`SEAL: ACCEPTED` = converged):

| Priority | Class | Condition |
|----------|-------|-----------|
| 1 | VOIDED | a `VOIDED` marker file is present in the runtime dir |
| 2 | SEALED | handoff status is `sealed` |
| 3 | ESCALATED | handoff status is `escalated` |
| 4 | STALL | running AND latest ledger row older than `--stall-min` minutes (default 30) |
| 5 | OSCILLATION | running AND review findings flat or rising across the last 2 review rows |
| 6 | RUNNING | running, recent, findings falling |

Hand ledgers report the tolerant status of their last anchored token line: SEALED,
CLEAN, REWORK, or UNKNOWN (no anchored `VERDICT:`/`SEAL:` line found).

## Invocation

```bash
bash ~/.claude/scripts/swarm/swarm-observe.sh                 # full text tables
bash ~/.claude/scripts/swarm/swarm-observe.sh --stall-min 15  # tighter stall threshold
bash ~/.claude/scripts/swarm/swarm-observe.sh --json          # one JSON object
bash ~/.claude/scripts/swarm/swarm-observe.sh --self-test     # fixture self-check, exit 0/1
```

Exit 0 whenever the scan completes; exit 1 only on an internal error. `--json` emits
`{"loops":[...],"workers":{...},"ephemeral":[...]}` with the same data as the tables.

## Reading the output

```
LOOP                         KIND   STATUS      ROUND.PHASE  TREND REVISION            AGE
b31-spawnlog-hardening-r2    driver SEALED      1.terminal   -     commit:288de3b4dbb8 0m
b31-spawnlog-hardening       driver ESCALATED   1.escalate   -     sha256:3a9dc8d2b532 15h
wf-skills                    hand   SEALED      -            -     -                   1d
```

Read it as: the r2 driver loop reached its terminal seal bound to commit `288de3b4dbb8`
(TREND `-` because a seal round carries no findings trend); the earlier r1 loop escalated
15 hours ago; the wf-skills hand ledger is sealed. A STALL or OSCILLATION row on a
`running` loop is the actionable signal to hand to the conductor supervising that loop.

## Constraints

- NEVER mutate a watched loop, its ledger, or any handoff. The parser opens nothing for
  writing outside `--self-test` (which writes only under its own tempdir).
- NEVER author or reinterpret a VERDICT or SEAL token; the tool QUOTES the tokens the
  loop's reviewers wrote, it never mints or edits them (`verdict-schema.md`, Provenance).
- NEVER seal or approve anything. This is an observer; convergence decisions belong to the
  loop's own fresh auditor (no pre-approval, R-5).
- NEVER kill a process. A wedged-looking loop is surfaced, not intervened on.
- READ-ONLY over every source, echoing the `/wf-watchdog` guarantee.

## Cross-References

- `/wf-watchdog` (B5 stall/oscillation doctrine this tool mechanizes; single-loop supervisor)
- `/swarm-status` (sub-second in-flight process snapshot sibling; this skill owns loop health)
- Converge ledger row schema: `~/.claude/plans/wf-autonomy/checkpoints/w1-design.md` (Ledger row schema)
- Data source formats: `~/.claude/plans/wf-autonomy/checkpoints/p0-ledgers.md`
- Verdict tokens + severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Campaign plan: `~/.claude/plans/wf-autonomy/plan.md`
