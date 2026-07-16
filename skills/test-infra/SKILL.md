---
name: test-infra
description: "Use when running the infrastructure regression tests."
category: meta
user-invocable: true
argument-hint: "[--quick | --component <name>] [--loop]"
allowed-tools: Read, Bash
---

# Test-Infra: Infrastructure Regression Suite

Run the regression test suite and display results, suggesting fixes on failure. The skill has two roles. By default it is a deterministic CHECKER: it runs the suite once and reports a pass/fail count. Under `--loop` it becomes a `/converge` binding for a deterministic gate, driving a fix-and-re-run loop until the named component reports zero failures.

**Arguments**: `$ARGUMENTS`, optional flags.

## Steps

1. Run the test suite:
   ```bash
   bash $HOME/.claude/scripts/infra-test.sh $ARGUMENTS
   ```
   Default runs `--full` (all categories). Pass `--quick` for hooks+settings only, or `--component <name>` for a single category. The script exits 0 when every check passes and exits 1 when any `FAIL` line is emitted; that exit code is the deterministic gate.

2. Display the output directly.

3. If any `FAIL` lines appear, suggest fixes:
   - **ST (settings)**: JSON parse → `jq .`, deny floor → check for deleted deny rules
   - **H (hooks)**: module syntax → `bash -n` on failing module, test suite failure → run `test-hooks.sh` standalone
   - **A (agents)**: frontmatter → add `---` delimiters, model → use opus/sonnet/haiku, symlinks → check target exists, skill refs → verify skill dir; **A6 grants** → restore SendMessage/Skill/WebSearch/WebFetch on meta.md, SendMessage/Skill on orch.md; **A7 fleet** → each w-*.md needs exactly one `## Report Contract (wf-skills)` section, Skill on the 10 reasoning workers (incl. w-hostile-reviewer), absent from w-committer/w-explorer
   - **S (skills)**: missing SKILL.md → create it, tool names → use Read/Write/Edit/Bash/Glob/Grep/Agent; **S6 _shared** → restore the 8 rubric blocks with a `Consumed by:` line, no em/en-dash; **S7 new skills** → converge/review-dispatch/hostile-review need name/description/user-invocable and no `disable-model-invocation`; **S8 flip invariant** → remove any re-added `disable-model-invocation: true` from skills/*/SKILL.md (checked on disk, incl. gitignored nudge); **S9 destructive gates** → restore exactly one `## Unattended-context gate` heading and a `description:` starting "Use when the user explicitly" on skills/git, skills/session-reaper, skills/handoff; **S10 schedule skills** → the 5 wf-* drivers (wf-wave-monitor, wf-watchdog, wf-hpc-watch, wf-nb-watch, wf-hygiene) each need a "Use when" description, category: workflow, user-invocable: true, no disable-model-invocation, and exactly 6 `## ` headers (R-1 template); **S11 wf-family** → every wf-*/SKILL.md carries category: workflow and no disable-model-invocation (>= 8 in the family); **S12 loop-integration** → restore the `## Loop integration` section on the converted Class-A skills (or `### Convergence loop` on research/references/gap-audit.md); **S13 no-self-seal** → review/design-review/sanity-check keep "never seals itself" and carry no positive "is the seal" construction; **S14 token grammar** → restore the VERDICT + SEAL grammar lines, the `## Canonical emitted /goal block` heading, and a `/goal Accept only when` line in `_shared/verdict-schema.md`; **S15 use-when descriptions** → every `skills/*/SKILL.md` description must start "Use when" and be <= 80 chars (checked on disk incl. gitignored); **S16 schema binding** → converge and review-dispatch must each cite `verdict-schema`
   - **R (rules)**: heading → start with `# Title`, numbering → rename file prefix
   - **C (comms)**: missing files → create 4-file set, agent xref → verify agent definition exists
   - **WF (wf-skills scripts)**: missing/non-exec/`bash -n` → check `scripts/comms/*.sh`, `scripts/decontaminate.sh`, `scripts/swarm/recover-worker.sh`; behavioral → broker-queries refuses unknown verbs (exit 1), decontaminate flags forbidden tokens (exit 1) and passes clean files (exit 0), hcom-send.sh (WF5) constructs a well-formed `send` argv under a stub HOME (never touches the real broker), recover-worker.sh (WF6) exits nonzero + prints usage on no-args / missing file

## Components

| Flag | Tests | Time |
|------|-------|------|
| (none) / `--full` | All: ST + H + A + S + R + C + M + SK + WF | ~10s |
| `--quick` | H + ST (hooks + settings) | ~2s |
| `--component agents` | A only (incl. A6 grants, A7 fleet contract: 10 reasoning workers incl. w-hostile-reviewer) | <1s |
| `--component skills` | S only (incl. S6 _shared, S7 converge/review-dispatch/hostile-review, S8 flip invariant, S9 destructive gates, S10-S13 wf-skills schedule/family/loop/no-self-seal, S14 token grammar, S15 use-when descriptions, S16 schema binding) | <1s |
| `--component rules` | R only | <1s |
| `--component comms` | C only | <1s |
| `--component wfscripts` | WF only (comms/swarm/decontaminate scripts; WF1-WF4 structural + broker/decontaminate behavioral, WF5 hcom-send construct, WF6 recover-worker guards) | <1s |

## Loop integration (converge)

`--loop` turns the checker into a thin `/converge` binding for a DETERMINISTIC gate. Read `/converge` first; this section states only the deltas. Loop orchestration (dispatching the fix producer and printing the `/goal` block) runs in the conductor's context (meta/orch, which holds Agent and Skill); this skill's allowed-tools cover only its single invocation.

**Authority**: the plain checker (`--quick` / `--component` / `--full`) is usable by anyone. `--loop` dispatches fix workers, so it is meta + orch only; a worker cannot spawn children, and invoking `--loop` from a `w-*` is a no-op error.

**Deterministic gate, not a VERDICT.** test-infra emits a machine-checked pass/fail count, never an LLM token. Per `verdict-schema.md` (deterministic-checker row), any `FAIL` maps to a failed gate (blocking-class): there is no major or minor gradation, and no VERDICT or SEAL line. The gate evidence is the exit code and the `Fail: N` summary from the conductor's OWN run, never a producer's "it passes now" claim (loop rule c, tool-verified critique).

**Loop body (per round).** `--loop` requires `--component <name>`, so the fix and the re-run target the same surface:

1. **GATE**: the conductor runs `bash $HOME/.claude/scripts/infra-test.sh --component <name>` itself. Zero `FAIL` (exit 0) meets the gate; the loop has already converged.
2. **DISPATCH FIX**: on one or more `FAIL` lines, the conductor dispatches ONE fix producer per `dispatch-contract.md`, routed by failure shape:
   - content, frontmatter, missing-file, or restore-a-block failures (ST, A, S, R, C, and missing or non-exec WF scripts) → `w-implementer`;
   - syntax, behavioural, and test-suite failures (H, `bash -n`, and behavioural WF checks) → `w-debugger`.
   The dispatch prompt carries the exact `FAIL` lines plus the matching per-category remediation from step 3 above as the punch list. The producer edits, then returns `STATUS: DONE`; it never certifies the gate itself.
3. **RE-RUN**: the conductor re-runs the SAME `--component <name>` and reads the fresh count. This re-run is the sole acceptance evidence and must post-date the fix (doctrine delta 7, no pre-approval): a green run recorded before the last change never fires the goal.
4. **REPEAT**: loop steps 1 to 3 until zero `FAIL` or the round cap (default 4). If the `FAIL` count does not fall across 2 consecutive rounds, ESCALATE rather than looping further (stall or oscillation).

There is no LLM reviewer and no SEAL: the deterministic 0-`FAIL` gate replaces the fresh-auditor SEAL, and the two independent exit signals are that gate (the conductor's own re-run) and the fix producer's separate `STATUS: DONE`.

## Emitted /goal block

Like every `/converge` binding, `--loop` ENDS setup by printing a ready-to-paste `/goal` block in the canonical shape (`verdict-schema.md`, Canonical emitted /goal block), specialised for the deterministic goal, then STOPS. It never arms `/goal` or `/loop` itself (DEC-R2); the human pastes `/goal` to arm the engine. No LLM SEAL clause appears, because the gate is purely deterministic:

```
/goal Accept only when ALL hold: (1) the transcript contains the conductor's own re-run of `bash ~/.claude/scripts/infra-test.sh --component <name>` reporting 0 FAIL (exit 0), stated to be the MOST RECENT such run and to post-date the last change to the infra files (no stale green run); (2) the fix producer has separately stated completion (STATUS: DONE); (3) no separate LLM SEAL is required, the deterministic 0-FAIL gate replaces the fresh-auditor seal for this purely deterministic check. If fix rounds exceed 4 (the default cap), or the FAIL count does not decrease across 2 consecutive rounds, declare ESCALATE and stop.
```

Paste this to arm the engine; test-infra does not self-arm.

## Constraints

- **NEVER** accept a producer's self-reported pass as gate evidence: the conductor re-runs the component itself every round (loop rule c).
- **NEVER** arm `/goal` or `/loop` yourself; print the block and stop (DEC-R2).
- **NEVER** author a VERDICT or SEAL token for this gate: it is deterministic, so its acceptance signal is a `FAIL` count, not a reviewer opinion (`verdict-schema.md`, deterministic-checker row).
- **NEVER** invoke `--loop` from a `w-*` worker; only meta and orch can spawn the fix producers.
- The plain checker behaviour (`--quick` / `--component` / `--full`, the Components table, the step-3 remediation) is unchanged; `--loop` is strictly additive.

## Cross-References

- Engine mechanics, the 8 loop rules, ledger, goal-string emission, DEC-R2: `~/.claude/skills/converge/SKILL.md`
- Token protocol, severity mapping, deterministic-checker row (test-infra FAIL = failed gate): `~/.claude/skills/_shared/verdict-schema.md`
- Dispatch contract plus model split for the fix producer: `~/.claude/skills/_shared/dispatch-contract.md`
- Sibling deterministic-gate binding (LaTeX compile check): `~/.claude/skills/wf-report/SKILL.md`
