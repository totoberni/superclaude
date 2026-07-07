---
name: wf-websearch
description: "Use when driving iterative multi-agent web research to a saturation-and-hostile-review seal. Meta+orch only."
category: workflow
user-invocable: true
argument-hint: "<research question> [--waves N] [--searches-per-wave 5] [--strict]"
allowed-tools: Read, Write, Edit, Bash, Agent, Skill
---

# wf-websearch

Convergence binding for iterative web research. Each round runs a WAVE of parallel web-search subagents; the conductor folds their returns into one synthesis document; a hostile reviewer audits that synthesis. Rounds repeat until the research SATURATES (a wave adds no new must-read sources) AND the synthesis passes a clean hostile seal. wf-websearch configures and runs the waves; the printed `/goal` block lets the external engine enforce the exit. The skill never arms the engine itself.

**Authority**: meta + orch only. Workers (`w-*`) cannot spawn the search wave or the reviewer, so invoking wf-websearch from a worker is a no-op error. The conductor (meta or orch) owns the loop, is the SOLE writer of the synthesis document, quotes verdicts, and maintains the ledger.

## What this binds

wf-websearch is a thin binding of `/converge` on binding **B1** (goal-sealed convergence). It fixes the three generic converge slots to web research:

- **Artifact**: one synthesis document, the running answer to the research question. The conductor is its only editor; searchers RETURN their findings and the conductor MERGES them (concurrent append-to-shared races collapse the artifact into colliding sections and dangling cross-references).
- **Producer (per round)**: a WAVE of up to 5 parallel `general-purpose` web-search subagents, dispatched through the `/swarm-dispatch` mixed-batch (W-7) pattern. Each searcher answers one bounded sub-question and RETURNS its sources; the conductor synthesises the returns into the artifact.
- **Reviewer (per round)**: `w-hostile-reviewer`, resolved via `/review-dispatch` (methodology class, `--scope methodology`), auditing the synthesis for source quality, contradictions between sources, coverage gaps, and unsupported novelty claims. The scope is stated explicitly because a code-free web synthesis must not drag the technical gauntlet; the methodology class otherwise defaults to `--scope both`.

Loop mechanics (round order, round ledger, two-token protocol, caps, post-compaction requote) are inherited from `/converge`; this file states only the wave-specific loop body and the dual predicate.

## Loop body (per wave/round)

Each round is one search wave followed by one synthesis audit. Six steps in order:

1. **PRODUCE**: dispatch up to `--searches-per-wave` (default 5, HARD CAP 5) parallel `general-purpose` searchers in a single Agent-tool batch. Each prompt carries the four-part dispatch contract plus: one bounded sub-question, a word cap (<=300 words), the search-budget discipline (cite URL + title + one-line takeaway per source; time-box each query 2-3 min), and a pre-committed (R-1) return schema `{sub-question, sources:[{url,title,takeaway}], gaps:[...]}` so the conductor can fold every return uniformly. Searchers are producers: they NEVER emit a verdict token.
2. **SYNTHESISE**: the conductor (sole editor) folds the wave's returns into the running synthesis document, de-duplicating sources already cited and reconciling contradictions rather than appending blindly. Re-derivation of an already-cited source across searchers is a saturation signal, not new coverage.
3. **PERSIST**: write the synthesis document to disk, then append a ledger entry (round, sub-questions issued, NEW must-read sources this wave, running source count) before any review runs. Load-bearing content lives on disk, never only in the final message.
4. **REVIEW**: resolve `w-hostile-reviewer` through `/review-dispatch` on the synthesis, `--scope methodology` (artifact + diff + rubric only; reviewer isolation). It emits a `VERDICT` line.
5. **REPORT**: the conductor quotes the reviewer's `VERDICT` line verbatim into the transcript and the ledger. Only the reviewer authors tokens; the conductor relays them.
6. **TRIAGE**: accepted coverage gaps and the reviewer's unanswered questions become the NEXT wave's bounded sub-questions. A wave that yields zero new must-read sources is logged explicitly as the saturation signal.

## Goal predicate

The loop converges only on a DUAL stop criterion; BOTH must hold in the same round:

- **(a) SATURATION** (first-class stop signal): the most recent wave surfaced ZERO new must-read sources. Independent re-derivation of an already-cited source is evidence of saturation, not new coverage. Saturation is stated explicitly in the ledger and the transcript; it is never inferred silently. This is the producer-side completion signal, independent of the reviewer.
- **(b) CLEAN SEAL**: a FRESH `w-hostile-reviewer` over the whole synthesis returns `blocking=0 major=0 minor=0`; `nits=0` is additionally required at the gate/strict bar, and `--strict` further requires two consecutive clean SEALs (`verdict-schema.md`, Bar levels).

Saturation without a clean seal means the answer is complete but under-audited: keep reviewing. A clean seal without saturation means the audit passed but coverage may be thin: run another wave. Only the two signals together terminate the loop.

## Emitted /goal block

Setup ENDS by printing a ready-to-paste `/goal` block, then STOPS (DEC-R2: the external judge stays independent; wf-websearch NEVER arms `/goal` itself). The human pastes it to arm the engine. Template:

```
/goal Accept only when ALL hold: (1) the transcript contains a line beginning "SEAL: ACCEPTED" that the conductor states is quoted verbatim from a FRESH w-hostile-reviewer return (scope methodology), is the MOST RECENT such line, and post-dates the last change to the synthesis document, reporting blocking=0 major=0 minor=0 (nits=0 at gate/strict); (2) the most recent search wave surfaced zero new must-read sources (saturation), which the conductor states in-transcript as the producer-side completion signal (a search loop has no separate STATUS: DONE; saturation is its producer-completion). If review rounds exceed <N> (from --waves, else 4), or total findings do not decrease across 2 consecutive rounds, declare ESCALATE and stop.
```

Print the block, then stop. The human pastes `/goal` to arm the engine.

## Constraints

- **NEVER** exceed 5 searchers per wave (Anthropic Agent-tool batch cap); more than 5 sub-questions means another wave, not a bigger batch.
- **NEVER** bundle uncertain calls (a source that may 404, a ref lookup) with the parallel searcher batch; Anthropic cancels ALL siblings if one errors. Run discovery first, dispatch from confirmed inputs.
- **NEVER** exceed the web-search budget once set (`_shared/search-budget.md`); a non-converging query is logged inconclusive and dropped, not deepened.
- **NEVER** let a searcher (producer) emit a `VERDICT` or `SEAL` token; only `w-hostile-reviewer` authors tokens, quoted verbatim by the conductor.
- **NEVER** let searchers write the synthesis document concurrently; the conductor is its sole editor.
- **NEVER** arm `/goal` yourself; print the block and stop (DEC-R2).
- **NEVER** invoke wf-websearch from a `w-*` worker; only meta and orch hold spawn authority.

## Cross-References

- Loop engine + round mechanics: `/converge` (binding B1)
- Wave dispatch pattern (W-7 mixed-batch): `/swarm-dispatch`
- Reviewer resolution (methodology class): `/review-dispatch`
- Per-wave synthesis auditor: `/hostile-review` (run by `w-hostile-reviewer`)
- Web-search budget discipline: `~/.claude/skills/_shared/search-budget.md`
- Verdict tokens + severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Campaign plan: `~/.claude/plans/wf-skills/plan.md`
