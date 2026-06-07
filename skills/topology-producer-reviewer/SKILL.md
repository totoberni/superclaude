---
name: topology-producer-reviewer
description: "Producer-Reviewer dyad: pair worker with reviewer audit. Foreground or BG (W-4)."
category: delegation
user-invocable: true
argument-hint: "<producer-type> <reviewer-type> [--bg] <task>"
allowed-tools: Read, Bash, Grep, Glob, Agent
---

# Producer-Reviewer Dyad

Pair every implementer worker with a reviewer worker that audits the producer's output. Reusable building block; runs in foreground (sequential) or background (W-4 overlap).

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
5. If verdict = FAIL → fix or re-delegate; if PASS → return

### Background (`--bg`, Pattern W-4)

1. Spawn producer K
2. After K returns, immediately dispatch reviewer K with `run_in_background: true`
3. Proceed to spawn producer K+1 (overlap)
4. Notification arrives when reviewer K completes — read its verdict
5. After all producers + reviewers done, synthesize verdicts into final report

## Producer-Reviewer Pairings

| Producer | Reviewer | When |
|----------|----------|------|
| `w-implementer` | `w-reviewer` | Code addition |
| `w-doc` | `w-reviewer` (prose mode) | LaTeX/doc polish |
| `w-refactorer` | `w-reviewer` (code mode) | Refactoring |
| `w-merger` | `w-reviewer` | Merge conflict resolution |
| `general-purpose` (research) | `w-reviewer` + `general-purpose` (cross-check) | Research synthesis |

## Critical Rules

- **Reviewer model can DIFFER from producer model** — e.g., sonnet producer + opus reviewer for high-stakes work. Mismatch is a feature, not a bug.
- **--bg back-out risk**: reviewer K's verdict may arrive AFTER producer K+1 has spawned. If reviewer K = REJECT, be prepared to back out producer K+1's work (it built on rejected foundation).
- **Apply auto-baseline-stash (R-2)** for `/commit false` repos when dispatching reviewer — prevents dirty-tree attribution false-positive REJECTs.
- **Embed thinking keyword in EACH worker's spawn prompt** — SOT: `rules/13-worker-first-mandate.md` § Critical Implementation Note.
- When authoring spawn prompts, keep `.workflow` / `/.deep-research` / `.ultracode` dot-escaped (see `rules/13-worker-first-mandate.md` § Trigger Escaping (Author-Time)).
- **Reviewer is read-only** — fixes route through a NEW producer or escalation, never via the reviewer itself.

## Output Format

Producer-Reviewer cycle log:

```
Cycle K:
  Producer: <type> | model: <m> | status: DONE/FAIL | time: Ns
  Reviewer: <type> | model: <m> | status: DONE/FAIL | verdict: PASS/REJECT/CONDITIONAL | time: Ns
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
- **NEVER skip reviewer** for "small" changes — defeats the pattern. If it's truly trivial, don't invoke this skill.
- Reviewer is read-only audit only. Fixes route through a new producer or escalation, never reviewer-as-fixer.
- Pattern is a **building block**, not a batch dispatcher. For mixed batches of N independent dyads, use `/swarm-dispatch` and let it embed dyads via this skill.

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
- Related skill: `/swarm-dispatch` (uses this dyad as a building block within mixed-shape batches)
- Hierarchy + write scopes: `~/.claude/rules/12-agent-hierarchy.md`
- R-4 closure: `/promote` skill scans the ledger above and drafts promotions when threshold is hit
