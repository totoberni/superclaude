---
name: topology-producer-reviewer
description: "Use when pairing a producer worker with a reviewer audit dyad."
category: delegation
user-invocable: true
argument-hint: "<producer-type> <reviewer-type> [--bg] [--rounds N] <task>"
allowed-tools: Read, Bash, Grep, Glob, Agent
---

# Producer-Reviewer Dyad

Pair every implementer worker with a reviewer worker that audits the producer's output. Reusable building block; runs in foreground (sequential) or background (W-4 overlap).

This is the SINGLE-round dyad primitive: one producer plus one reviewer, one pass. It does not loop and never seals itself. To iterate produce-then-review until the reviewer verdict is clean, the conductor repeats this dyad through `/converge` (see Loop integration below).

**Authority**: meta + orch only.

**Args**: $ARGUMENTS

## When to Use

- Producing high-stakes artefact (architecture decisions, security-relevant code, irreversible changes)
- Code/doc that benefits from immediate independent audit (catch regressions before next batch)
- Build-up phase where each producer's output feeds the next (reviewer catches issues before they propagate downstream)

## Modes

| Mode | Behavior | Wall savings | Use for |
|------|----------|--------------|---------|
| `--sequential` (default) | Producer → wait → Reviewer → wait → return | 0% (baseline) | Critical first-pass; want reviewer findings before continuing |
| `--bg` (W-4 pattern) | Producer K → spawn Reviewer K with `run_in_background: true` → proceed to Producer K+1; Reviewer K reports back asynchronously | ~40% | Pipelined work; willing to fix Reviewer K-1 findings while Producer K runs |

## Procedure

### Sequential (`--sequential`, default)

1. Spawn producer (model per matrix in `13-worker-first-mandate.md`, scope per task)
2. Wait for producer return
3. Spawn reviewer (`w-reviewer` for code; `w-doc` + `w-reviewer` combo for prose)
4. Wait for reviewer return
5. The reviewer emits a VERDICT line (`VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K`, per `_shared/verdict-schema.md`). If REWORK (blocking or major > 0), fix or re-delegate; if CLEAN, return.

### Background (`--bg`, Pattern W-4)

1. Spawn producer K
2. After K returns, immediately dispatch reviewer K with `run_in_background: true`
3. Proceed to spawn producer K+1 (overlap)
4. Notification arrives when reviewer K completes; read its VERDICT line
5. After all producers + reviewers done, synthesize verdicts into final report

## Producer-Reviewer Pairings

| Producer | Reviewer | When |
|----------|----------|------|
| `w-implementer` | `w-reviewer` | Code addition |
| `w-doc` | `w-reviewer` (prose mode) | LaTeX/doc polish |
| `w-refactorer` | `w-reviewer` (code mode) | Refactoring |
| `w-merger` | `w-reviewer` | Merge conflict resolution |
| `general-purpose` (research) | `w-reviewer` + `general-purpose` (cross-check) | Research synthesis |

## Loop integration (converge)

One topology dyad is exactly ONE `/converge` round: a single PRODUCE (or REVISE) step followed by a single REVIEW step. This skill is the per-round dyad primitive that `/converge` repeats; it does not loop, and it never seals itself.

To iterate produce-then-review until the reviewer verdict is clean (iterate-to-PASS), the conductor (meta or orch) drives the repetition through `/converge`, which runs this dyad as its per-round PRODUCE+REVIEW step and terminates only on a fresh auditor's `SEAL`. Do not reimplement a convergence loop here; point multi-round iteration at `/converge`.

Loop orchestration (repeating the dyad, quoting each VERDICT line, maintaining the round ledger, and spawning the fresh seal auditor) runs in the conductor's context, which holds Agent + Skill. This skill declares in its own `allowed-tools` only what a single dyad invocation needs (Agent, to spawn the producer and reviewer); the loop is conductor-driven, not self-driven.

**Verdict vocabulary.** The reviewer emits a VERDICT line per `_shared/verdict-schema.md`: `VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K`. This skill's PASS/REJECT/CONDITIONAL synthesis vocabulary maps onto that token:

| Synthesis bucket | VERDICT line |
|---|---|
| PASS | CLEAN (blocking=0, major=0) |
| REJECT | REWORK with blocking or major > 0 |
| CONDITIONAL | REWORK if any residual major (it gates; next-round punch list); a residual minor-only is advisory (non-gating at the default bar, gating at the gate/strict bar) |

Only the reviewer authors the token; the conductor quotes it verbatim. A round reviewer never seals its own round: the terminal `SEAL` always comes from a fresh auditor of a DIFFERENT identity than any round reviewer (verdict-schema, Provenance + No pre-approval).

**`--rounds N` shorthand.** `--rounds N` (N > 1) does not loop within this skill. It signals the conductor to drive the iteration through `/converge <target> --rounds N`, which repeats this dyad to a sealed finish; never build a bespoke self-sealing loop here. With no `--rounds` flag (or `--rounds 1`), this skill runs exactly one dyad and returns.

## Critical Rules

- **Reviewer model can DIFFER from producer model**: e.g., sonnet producer + opus reviewer for high-stakes work. Mismatch is a feature, not a bug.
- **--bg back-out risk**: reviewer K's verdict may arrive AFTER producer K+1 has spawned. If reviewer K = REJECT, be prepared to back out producer K+1's work (it built on rejected foundation).
- **Apply auto-baseline-stash (R-2)** for no-commit-projects convention repos when dispatching reviewer; prevents dirty-tree attribution false-positive REJECTs.
- **Embed thinking keyword in EACH worker's spawn prompt**: SOT: `rules/13-worker-first-mandate.md` § Critical Implementation Note.
- When authoring spawn prompts, keep `.workflow` / `/.deep-research` / `.ultracode` dot-escaped (see `rules/13-worker-first-mandate.md` § Trigger Escaping (Author-Time)).
- **Reviewer is read-only**: fixes route through a NEW producer or escalation, never via the reviewer itself.

## Output Format

Producer-Reviewer cycle log:

```
Cycle K:
  Producer: <type> | model: <m> | status: DONE/FAIL | time: Ns
  Reviewer: <type> | model: <m> | status: DONE/FAIL | VERDICT: <CLEAN|REWORK ...> (bucket: PASS/REJECT/CONDITIONAL) | time: Ns
```

Final synthesis:

```
Cycles: N total | PASS: P | REJECT: R | CONDITIONAL: C
Action items:
  - [items from REJECT verdicts]
  - [items from CONDITIONAL verdicts]
Wall time: Ts (savings vs sequential: NN%)
```

## Constraints

- **NEVER use --bg mode** if producer K+1 can be poisoned by producer K's bad output. Sequential is safer when producers chain.
- **NEVER skip reviewer** for "small" changes: defeats the pattern. If it's truly trivial, don't invoke this skill.
- Reviewer is read-only audit only. Fixes route through a new producer or escalation, never reviewer-as-fixer.
- Pattern is a **building block**, not a batch dispatcher. For mixed batches of N independent dyads, use `/swarm-dispatch` and let it embed dyads via this skill.
- **NEVER build a multi-round loop inside this skill**: one invocation is one dyad (one `/converge` round). To iterate produce-then-review to a sealed finish, delegate to `/converge`; the reviewer never seals its own round.

## R-4 Pattern Tracking

When the same producer-reviewer pairing recurs >= 3 times across sessions (e.g., `w-doc` + opus-`w-reviewer`), it's a candidate for a permanent named composition.

Track in the DB (`shared-global` tier) via:
```bash
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  upsert --tier shared-global --type reference \
  --name producer-reviewer-<producer>-<reviewer> \
  --description "Producer-Reviewer composition pattern tracking" \
  --text-stdin <<'EOF'
- composition: <producer>+<reviewer-with-model-override>
  occurrences: N
  example-context: "academic LaTeX cross-section coherence"
  recommended-promotion: <new-skill-name OR keep ephemeral>
EOF
```

After >= 3 occurrences, propose a named topology variant via Meta (e.g., `/topology-doc-deep-review` as a wrapper).

## Cross-References

- Battle-tested patterns (W-4 BG overlap and other W-* patterns): `~/.claude/rules/13-worker-first-mandate.md`
- Quality gates (R-1 schema, R-2 baseline-stash, R-3 verification, R-4 fleet expansion): `~/.claude/rules/40-swarm-quality-gates.md`
- Source pattern: Harness Producer-Reviewer (`github.com/revfactory/harness`)
- Loop engine: `/converge` (multi-round iterate-to-seal; this skill is its per-round PRODUCE+REVIEW dyad primitive; `--rounds N` here delegates to it)
- Verdict protocol SOT: `~/.claude/skills/_shared/verdict-schema.md` (the reviewer's VERDICT line; PASS/REJECT/CONDITIONAL mapping)
- Related skill: `/swarm-dispatch` (uses this dyad as a building block within mixed-shape batches)
- Hierarchy + write scopes: `~/.claude/rules/12-agent-hierarchy.md`
- R-4 closure: `/promote` skill scans the ledger above and drafts promotions when threshold is hit
