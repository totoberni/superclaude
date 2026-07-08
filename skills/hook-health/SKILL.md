---
name: hook-health
description: "Use when scoring hook subsystem health; syntax, perf, coverage."
model: haiku
category: health
user-invocable: true
argument-hint: "[--quick | --standard | --deep] [hook-name | all]"
allowed-tools: Read, Bash, Glob, Grep, Agent
---

# Hook Health Assessment (/100)

Score `~/.claude/hooks/` health. Read-only for `--quick`; `--standard`/`--deep` spawn reviewers. Under `--deep`, a conductor can drive it as a `/converge` iterate-to-clean loop for a deterministic gate (see Loop integration).

**Target**: $ARGUMENTS (default: `--quick all`)

## Scoring Criteria

| # | Criterion | Pts | Tiers | Measurement |
|---|-----------|-----|-------|-------------|
| 1 | Syntax (`bash -n`) | 20 | all | `passing / total * 20` |
| 2 | Permissions (`+x`) | 10 | all | `executable / total * 10` |
| 3 | Performance <500ms | 15 | all | `fast / total * 15` |
| 4 | No hardcoded paths | 10 | all | `clean / total * 10` |
| 5 | Cleanup patterns | 10 | all | `covered / total_temp_types * 10` |
| 6 | Module naming `NN-*.sh` | 5 | all | `compliant / total * 5` |
| 7 | Graceful degradation | 10 | std+ | Binary: dispatcher exit 0 with missing module |
| 8 | Test coverage | 15 | std+ | `tested / total * 15` (refs in test-hooks.sh) |
| 9 | No `set -e` | 5 | all | Binary: any match = 0 |

## Implementation (canonical runner)

`bash ~/.claude/scripts/hook-health.sh $ARGUMENTS` is the authoritative deterministic
implementation of all 9 criteria below. It supports `--quick|--standard|--deep`, prints a
per-criterion breakdown, and emits a final `SCORE: <int>/100` line. Criteria 7 (graceful
degradation) and 8 (test coverage) are deterministic and self-contained (graceful = the
dispatcher exits 0 with a module removed from a throwaway copy of the hooks tree;
coverage = grep of `test-hooks.sh`); the script never spawns agents. Run it and present
its output. The criteria table below documents what the script implements.

## Quick Checks (all tiers)

```bash
HOOKS_DIR="$HOME/.claude/hooks"
# 1. Syntax
for f in "$HOOKS_DIR"/*.sh "$HOOKS_DIR"/modules/*.sh; do [ -f "$f" ] && bash -n "$f" 2>&1; done
# 2. Permissions
for f in "$HOOKS_DIR"/*.sh "$HOOKS_DIR"/modules/*.sh; do [ -f "$f" ] && ([ -x "$f" ] && echo "OK: $(basename "$f")" || echo "MISS: $(basename "$f")"); done
# 3. Performance (test dispatchers registered in settings.json, NOT pre-tool-use.sh)
for f in "$HOOKS_DIR"/*.sh; do [ -f "$f" ] || continue; START=$(date +%s%N); echo '{"session_id":"perf-test","tool_name":"Read"}' | timeout 1 bash "$f" >/dev/null 2>&1; END=$(date +%s%N); echo "$(basename "$f"): $(( (END-START)/1000000 ))ms"; done
# 4. Hardcoded paths
grep -rn "$HOME" "$HOOKS_DIR"/ 2>/dev/null | grep -v "^Binary"
# 5. Cleanup patterns (temp exts: start agent pid override context-warned tdd calls)
for ext in start agent pid override context-warned tdd calls; do grep -qE "\.$ext|[{,]${ext}([},])" "$HOOKS_DIR/session-cleanup.sh" 2>/dev/null && echo "OK: $ext" || echo "MISS: $ext"; done
# 6. Module naming
for f in "$HOOKS_DIR"/modules/*.sh; do basename "$f" | grep -qE '^[0-9]{2}-' || echo "BAD: $(basename "$f")"; done
# 9. No set -e
grep -rn "set -e" "$HOOKS_DIR"/ 2>/dev/null
```

## Standard Checks (--standard, --deep)

**7. Graceful degradation**: Rename one module → run dispatcher (`session-timer.sh`) with mock JSON → verify exit 0 → rename back. Note: the dispatcher is registered in `settings.json` under `PreToolUse` — do NOT hardcode `pre-tool-use.sh` (it doesn't exist).

**8. Test coverage**: Count hooks/modules referenced by name in `~/.claude/scripts/test-hooks.sh`. Score = tested/total * 15.

## Deep Checks (--deep only)

Spawn `w-debugger` to test: missing `$TIMER_DIR`, corrupted `.start` files, concurrent execution, empty JSON input. Warn about session budget (~15 min).

## Output

```
## Hook Health Report
**Score: NN/100** (tier: quick|standard|deep)
| # | Criterion | Score | Detail |
|---|-----------|-------|--------|
### Issues Found
### Recommendations
```

## Loop integration (converge)

Under `--deep`, hook-health can be driven as a thin `/converge` binding for a DETERMINISTIC gate: an iterate-to-clean loop that runs `--deep`, fixes the hook failures it surfaces, and re-runs `--deep` until no new failures remain and the score sits at its ceiling. Read `/converge` first; this section states only the deltas. Loop orchestration (dispatching the fix producer and printing the `/goal` block) runs in the conductor's context (meta/orch, which holds Agent and Skill); this skill's allowed-tools cover only its single invocation, including the existing w-debugger edge-case test spawn in Deep Checks.

**Authority**: a single `--quick` / `--standard` / `--deep` run is usable by anyone. The iterate-to-clean loop dispatches fix workers across rounds, so it is meta + orch only; a worker cannot spawn children, and driving the loop from a `w-*` is a no-op error.

**Deterministic gate, not a VERDICT.** hook-health emits a machine-computed `SCORE: <int>/100` and a concrete list of hook failures, never an LLM token. Per `verdict-schema.md` (deterministic-checker row), a score-gate result maps to a failed gate (blocking-class): there is no major or minor gradation, and no VERDICT or SEAL line. The gate evidence is the conductor's OWN `--deep` re-run (its score and new-failure list), never a producer's "the hook is fixed now" claim (loop rule c, tool-verified critique).

**Loop body (per round).** The loop targets `--deep` on the same hook scope (`hook-name` or `all`) each round:

1. **GATE**: the conductor runs `bash ~/.claude/scripts/hook-health.sh --deep <scope>` itself. The script computes the deterministic `SCORE: <int>/100` over the 9 criteria; the score at its ceiling with zero new criterion failures IS the gate, and the loop has converged. The `--deep` w-debugger edge-case tests (a separate skill/conductor action, not run by the script, which never spawns agents) are advisory: their findings feed the fix punch list but are NOT a gate condition, so the gate stays purely deterministic.
2. **DISPATCH FIX**: on one or more failed criteria, the conductor dispatches ONE `w-debugger` fix producer per `dispatch-contract.md`. The dispatch prompt carries the exact failing criteria plus any advisory `--deep` edge-case findings and the report Recommendations as the punch list. The producer edits the hook or module, then returns `STATUS: DONE`; it never certifies the gate itself.
3. **RE-RUN**: the conductor re-runs `--deep` on the same scope and reads the fresh score and failure list. This re-run is the sole acceptance evidence and must post-date the fix (doctrine delta 7, no pre-approval): a clean run recorded before the last change never fires the goal.
4. **REPEAT**: loop steps 1 to 3 until zero new criterion failures (score at its ceiling) or the round cap (default 4, set at the `/converge` level, never passed to the script). If the criterion-failure count does not fall across 2 consecutive rounds, ESCALATE rather than looping further (stall or oscillation).

There is no LLM reviewer and no SEAL: the deterministic ceiling-score / zero-new-failure gate replaces the fresh-auditor SEAL, and the two independent exit signals are that gate (the conductor's own `--deep` re-run) and the fix producer's separate `STATUS: DONE`.

## Emitted /goal block

Like every `/converge` binding, the `--deep` loop ENDS setup by printing a ready-to-paste `/goal` block in the canonical shape (`verdict-schema.md`, Canonical emitted /goal block), specialised for the deterministic goal, then STOPS. It never arms `/goal` or `/loop` itself (DEC-R2); the human pastes `/goal` to arm the engine. No LLM SEAL clause appears, because the gate is purely deterministic:

```
/goal Accept only when ALL hold: (1) the transcript contains the conductor's own re-run of `bash ~/.claude/scripts/hook-health.sh --deep <scope>` reporting zero new hook failures with the SCORE at its ceiling, stated to be the MOST RECENT such run and to post-date the last change to the hooks (no stale clean run); (2) the fix producer has separately stated completion (STATUS: DONE); (3) no separate LLM SEAL is required, the deterministic ceiling-score / zero-new-failure gate replaces the fresh-auditor seal for this purely deterministic check. If fix rounds exceed the cap (default 4, set at the `/converge` level and never passed to the hook-health script), or the criterion-failure count does not decrease across 2 consecutive rounds, declare ESCALATE and stop.
```

Paste this to arm the engine; hook-health does not self-arm.

## Constraints

- **NEVER** accept a producer's self-reported fix as gate evidence: the conductor re-runs `--deep` on the same scope itself every round (loop rule c).
- **NEVER** arm `/goal` or `/loop` yourself; print the block and stop (DEC-R2).
- **NEVER** author a VERDICT or SEAL token for this gate: it is deterministic, so its acceptance signal is a score and failure count, not a reviewer opinion (`verdict-schema.md`, deterministic-checker row).
- **NEVER** drive the iterate-to-clean loop from a `w-*` worker; only meta and orch can spawn the w-debugger fix producers.
- The single-run checker behaviour (`--quick` / `--standard` / `--deep`, the 9 scoring criteria, the score-out-of-100 contract, and the w-debugger edge-case test spawn in Deep Checks) is unchanged; the loop is strictly additive.

## Cross-References

- Engine mechanics, the 8 loop rules, ledger, goal-string emission, DEC-R2: `~/.claude/skills/converge/SKILL.md`
- Token protocol, severity mapping, deterministic-checker row (score gate = failed gate): `~/.claude/skills/_shared/verdict-schema.md`
- Sibling deterministic-gate binding (infra regression suite with `--loop`): `~/.claude/skills/test-infra/SKILL.md`
