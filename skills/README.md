# Superclaude Skill System: Reference

The skills under `~/.claude/skills/*/SKILL.md` (70 of them) are the invocable capability layer of superclaude. Each skill is a self-contained protocol, a slash command a user types (`/converge`) and, by default, a capability the model can select autonomously through the Skill tool. This campaign added one property to the whole fleet: every skill is now loop-composable, and a small set of them forms a convergence engine that iterates an artefact through adversarial review rounds to an independent seal. This file is the deep mechanical reference. The entry-point summary, worked examples, and the "which skill do I reach for" tour live in the global `~/.claude/README.md`; this file does not repeat them. It owns the mechanics: the frontmatter contract, the two-token protocol, the bar levels, the engine bindings, the reviewer-resolution table, the wf-* family, and how a skill is converted to a workflow skill.

## Skill anatomy

A skill is a Markdown file with YAML frontmatter followed by the protocol body. The frontmatter is the contract between the skill and the two things that invoke it (a human via slash, the model via the Skill tool).

| Key | Meaning |
|---|---|
| `name` | The slash command and Skill-tool name (`/converge`). |
| `description` | A `"Use when ..."` trigger, kept under ~80 characters. This string is what the model matches against to auto-select the skill, so it is written as a triggering condition, not a title. |
| `category` | Taxonomy bucket (delegation, workflow, meta, memory, orchestration, domain, health, code-quality, research, testing, comms, maintenance). Drives the catalogue below. |
| `user-invocable` | `true` (the default) makes the skill a slash command. Set `false` for model-only protocols the user should not fire directly (8 skills: `debugging`, `code-quality`, `delegate`, `verify`, `infra-security`, `gas-patterns`, `wsl-gotchas`, `test-cleanup-protocol`). |
| `disable-model-invocation` | Absent (the default) means the skill is both model-invocable AND loop-composable: the model may select it and a loop engine may drive it. Setting it `true` suppresses that. After the DEC-R3 flip the key was deleted fleet-wide, so no skill sets it `true` (that flip is what made every skill loop-composable, and infra-test S8 guards against a re-added `true`). One skill, `notebook`, still carries the key set explicitly to `false`, a redundant restatement of the default. |
| `allowed-tools` | The tool grant for ONE invocation of the skill. A skill bound into a loop declares only what its single invocation needs (see the conductor-context convention). |
| `argument-hint` | The usage arguments shown to the user. |
| `context: fork` + `agent: <w-*>` | Marks a skill that forks its work to a named subagent: `plan` to `w-planner`, `sanity-check` and `review` to `w-reviewer`. Such a skill is a per-round reviewer or producer, never a loop driver. |

Invocation has two paths that resolve to the same protocol: a user types `/name args` (slash), or the model emits a Skill-tool call `Skill(name)`. A loop engine drives a skill through the same Skill-tool path, from the conductor's context. Rubrics are read by path today; a reviewer agent may preload one via its own agent-definition `skills:` frontmatter (for example `w-hostile-reviewer` preloads the `hostile-review` gauntlet), which is an agent-def key, not a skill-file key.

## The convergence engine

`/converge` (see `converge/SKILL.md`) is the generic loop driver: produce, then review, then iterate, until an independent auditor seals the artefact. It operationalises the two-token protocol over the external `/goal` engine. Converge configures and runs the rounds; it PRINTS a ready-to-paste `/goal` block and stops, it never arms the engine itself (DEC-R2). Authority is meta and orch only; a worker cannot spawn reviewers.

Each round runs five steps in order (`converge/SKILL.md`, Iteration protocol): PRODUCE/REVISE (delegate the build or the punch-list fixes to a producer), PERSIST (producer writes to disk, conductor appends a ledger entry), REVIEW (resolve the reviewer through `/review-dispatch`), REPORT (conductor quotes the reviewer's token line verbatim into the ledger), TRIAGE (accept or contest each finding with `file:line` evidence; accepted findings become the next round's punch list).

The **two-token protocol** (SOT: `_shared/verdict-schema.md`) is the spine. Three line-1 tokens, each the first line of an agent's final message so it survives truncation:

- `VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K` from the per-round reviewer.
- `SEAL: ACCEPTED|REJECTED blocking=N major=N minor=N nits=N` from a FRESH holistic auditor only, never the round reviewer.
- `STATUS: DONE|PARTIAL|FAILED files=N checkpoint=<path>` from a non-reviewer producer.

Only reviewer subagents author VERDICT and SEAL; producers and the conductor never do. The conductor QUOTES a reviewer token verbatim, and the `/goal` evaluator accepts only conductor-quoted reviewer tokens.

The **8 loop rules** (`converge/SKILL.md`, a through h) govern every round:

- **a. Rubric-bound reviewers.** Every dispatch carries an explicit rubric; never "review this".
- **b. Reviewer isolation.** The reviewer sees artefact, diff, and rubric only, never the producer's reasoning or a prior clean verdict.
- **c. Tool-verified critique.** Reviewers RUN the tests, linters, and compile gates themselves; a producer self-report is never evidence of record.
- **d. Anti-hacking sweep.** An automatic blocking finding in any scope (weakened assertions, harness escapes, skipped coverage, diagnostic theatre), run every round without being asked.
- **e. Two-token protocol.** VERDICT per round; termination only from a SEAL by the fresh auditor; producers are forbidden either token.
- **f. Dual-condition exit.** A clean `SEAL: ACCEPTED` terminates the loop only together with the producer's separate `STATUS: DONE`, two independent signals.
- **g. Nit policy / bar levels.** The seal bar has three tiers (next section); minor counts always appear in the token line.
- **h. Caps.** Default `--rounds 4`; escalate if total findings do not decrease for two consecutive rounds; `--panel` (2-3 diverse-tier judges, majority vote) is reserved for irreversible gates (DEC-R5).

Two harness-level engines sit outside the skill fleet and are what a printed block arms. `/goal` is the convergence engine: a turn-level evaluator that reads the transcript and fires when its natural-language condition holds. `/loop` is the time engine: a recurring scheduler that re-runs a tick on an interval. A convergence skill emits a ready-to-paste `/goal` block (a monitor emits `/loop` plus `/goal`); the human pastes it to arm the engine. `/goal` takes a natural-language CONDITION, never a `/goal seal ...` pseudo-subcommand.

## Bar levels

The convergence bar has three tiers (SOT: `_shared/verdict-schema.md`, Bar levels). A wf-* binding or a `/converge` flag selects one. Each is a superset of the one above it.

| Bar | Selected by | Seal requires | Notes |
|---|---|---|---|
| default | (nothing) | `blocking=0 major=0` | minors and nits logged, do not gate after round 1 |
| gate | `--stakes gate`, or a gate campaign | `blocking=0 major=0 minor=0 nits=0` | everything gates; a single clean SEAL |
| strict | `--strict` | the gate bar AND two consecutive clean SEALs | submission-grade (DEC-R1); the added tightening is the second consecutive clean audit, each a fresh examination |

## No pre-approval

This is the campaign's hardest-won invariant and the core anti-rubber-stamp rule (`_shared/verdict-schema.md`). Every VERDICT and every SEAL derives from a fresh, explicit examination of the CURRENT state of the work, with evidence gathered THIS round. Approval never transfers across rounds: a reviewer may not approve round N+1 on the strength of round N, may not anticipate a future clean state, and may not rest on "my prior findings were addressed" without re-examining the current artefact and citing fresh `file:line` evidence. A SEAL binds to a specific artefact revision (name the commit hash or round); ANY change to the artefact after a SEAL voids it and requires a fresh SEAL. The conductor never quotes a stale token: the `/goal` evaluator may act only on a SEAL the conductor states is the MOST RECENT reviewer return AND post-dates the last change to the artefact.

It is enforced twice over: as a standing rule (R-5 in `rules/40-swarm-quality-gates.md`) and as a standing regression test (infra-test S13). This is not campaign-scoped guidance; it is permanent doctrine for all convergence and review work.

## Engine bindings B1-B5

`--binding` selects the loop's driving cadence (`converge/SKILL.md`, Engine bindings). Expensive adversarial review fires per completion, never per tick.

| Binding | When to use | Cadence |
|---|---|---|
| B1 goal-sealed convergence | Default. One artefact iterated to a clean seal. | Event: each producer or reviewer return advances the loop; the goal seal terminates. |
| B2 phased campaign | Multi-phase build with handoffs between fresh sessions. | Cron, or 30-60m phases; each phase a fresh session rehydrated from a handoff file. |
| B3 poll-then-act monitor | Watch an external signal, act when it changes. | Fixed 10-15m poll, or a dynamic `/loop` tick gated by a goal seal. |
| B4 gardener / maintenance | Ongoing upkeep with no fixed endpoint. | Dynamic bare `/loop`; a hot-editable `loop.md` steers it live. No seal, no goal predicate. |
| B5 watchdog | Supervise another loop's health. | Short fixed `/loop` reading the round-ledger heartbeat; escalates on stall or oscillation. |

The round ledger (`rounds.md`, default under the campaign plans dir) records timestamp, round number, delta, the quoted VERDICT line, and open-findings count per round. A B5 watchdog reads it as a heartbeat. After compaction, the conductor re-quotes the latest VERDICT line from the ledger before continuing.

## Reviewer resolution

`/review-dispatch` (see `review-dispatch/SKILL.md`) resolves an artefact class to the correct adversarial reviewer, model, rubric, and mode. It assembles a specified dispatch; it does not re-decide review policy.

| Artefact class | Reviewer | Model | Rubric | Mode |
|---|---|---|---|---|
| `code-small` (<=3 files) | w-reviewer | sonnet | `code-quality` | adversarial |
| `code-large` / architectural | w-reviewer | opus | `code-quality` | adversarial |
| `frontend` | w-design-reviewer | sonnet (opus cross-page) | `design-review` | adversarial |
| `methodology` / research / reports | w-hostile-reviewer | opus | `hostile-review` | adversarial |
| `infra` (`~/.claude` changes) | w-reviewer | sonnet / opus by scale | `infra-security` | adversarial |
| `figures` | figure-validate (no LLM) | n/a | `figure-validate` | deterministic |
| `test-integrity` | w-reviewer | sonnet | `sanity-check` (5-category) | adversarial |

Every resolved dispatch carries the four-part dispatch contract (`_shared/dispatch-contract.md`) plus reviewer isolation, the verdict contract quoted verbatim, a numeric budget with the model split, and the no-pre-approval instruction. `--panel` (or `--stakes gate`) assembles 2-3 diverse-tier judges (at least one opus) with non-overlapping rubric scopes and a majority vote, reserved for irreversible gates.

**Conductor-context convention.** Loop orchestration (dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, spawning the fresh seal auditor) runs in the CONDUCTOR's context, which holds Agent and Skill. A skill bound into a converge loop declares in its own `allowed-tools` only what its single invocation needs; its loop-integration section is conductor-driven, not self-driven. A skill that forks to a worker (`context: fork`) is therefore a round reviewer or producer, never the loop driver, and never seals itself: the terminal SEAL always comes from a fresh auditor of a DIFFERENT identity than any round reviewer. Encoding this convention in the shared SOT the moment a review first revealed a self-seal removed the whole class of defect from every downstream binding.

## The wf-* family

Eight thin bindings that specialise `/converge` for a concrete artefact and cadence. Each PRINTS its ready-to-paste block and stops; none self-arms (DEC-R2). Three are flagship producing loops (B1); five are scheduled monitors and gardeners (B3, B4, B5).

| Skill | Binding | Purpose | Invocation |
|---|---|---|---|
| `wf-design` | B1 | Drive a research experimental design (13 `/research design` steps) to a `w-hostile-reviewer` methodology seal. | `/wf-design <phenomenon> [--rounds N] [--strict]` |
| `wf-report` | B1 | Drive a LaTeX report to a DUAL seal: a deterministic compile gate (`latexmk`, zero material warnings) plus a fresh `w-hostile-reviewer` SEAL. | `/wf-report <report-target> [--rounds N] [--strict] [--publish]` |
| `wf-websearch` | B1 | Drive multi-agent web research: waves of up to 5 parallel searchers, conductor-only synthesis, to a DUAL stop of saturation (zero new sources) plus a clean hostile seal. | `/wf-websearch "<question>" [--waves N] [--searches-per-wave 5]` |
| `wf-wave-monitor` | B3 | Meta polls the HCOM broker for orch health each interval; seals when every ork reports DONE with zero unanswered ESC. | `/wf-wave-monitor [--interval 15m] [--orchs <list|all>]` |
| `wf-watchdog` | B5 | Supervise another converge loop by reading its `rounds.md` heartbeat; escalate on stall or oscillation, note convergence on a latest `SEAL: ACCEPTED`. | `/wf-watchdog <ledger-path> [--interval 10m]` |
| `wf-hpc-watch` | B3 | Poll a long-running SLURM job read-only; act only on a RUNNING to DONE or FAILED transition; seal on a fresh terminal COMPLETED (plus an optional output audit). | `/wf-hpc-watch <job-id|job-name> [--interval 15m]` |
| `wf-nb-watch` | B3 | Watch a notebook run through `nb-monitor`'s progress file; on BROKEN or HUNG dispatch a fix round and re-run; seal on a clean completion plus a fresh reviewer. | `/wf-nb-watch <notebook-path> [--interval 10m]` |
| `wf-hygiene` | B4 | Gardener: a scheduled read-only pass over session health, memory-DB score, and checkpoint staleness. No artefact, no round cap, no SEAL; flags for the human only. | `/wf-hygiene [--interval daily]` |

Every B1 and B3 binding emits a `/goal` block whose clause 1 requires a fresh `SEAL: ACCEPTED` (most recent, post-dating the last change) and whose clause 2 is the producer-completion signal: `STATUS: DONE` for a build, a clean compile for `wf-report`, saturation for `wf-websearch`, all-orks-DONE for `wf-wave-monitor`, a fresh terminal SLURM state for `wf-hpc-watch`. B4 (`wf-hygiene`) has no goal predicate: it emits only a `/loop` block and runs until the human stops it. The monitors carry a durability caveat: `/loop` schedules die with the session, so a run that must outlive the session goes headless (`claude -p`) or onto the toto scheduling layer.

## Converting a skill to a workflow skill

The campaign mandate was convert-to-nature, not force-a-loop-everywhere. A skill gets exactly the loop integration its nature warrants; forcing a loop onto a one-shot skill is the diagnostic theatre the genuineness mandate forbids. Three treatments, chosen by what the skill actually does:

| Treatment | Applies to | Pattern |
|---|---|---|
| Full converge binding | Genuinely iterable artefacts and the reviewers that audit them | A `## Loop integration` section states how the skill plugs into `/converge`: a reviewer skill (`sanity-check`, `code-quality`, `design-review`, `hostile-review`, `infra-security`) declares its round-reviewer role and severity mapping and a conductor-driven `--loop` shorthand that prints the `/converge` invocation; a driver (`wf-design`, `wf-report`, `wf-websearch`) fixes the artefact, producer, and reviewer slots. Never self-seals. |
| Light single-self-check note | One-shot skills: retrospectives, advisory scans (`wrap-up`, `mistake`, `good-idea`, `verify`, `memory-search`) | A short note that the skill runs once and self-checks its own output; it does not spin a produce-review loop, and pretending otherwise would be theatre. |
| Deterministic gate | Computed-result checkers (`figure-validate`, `test-infra`, `hook-health`; the compile leg of `wf-report`) | Gates on a computed value (failed-count, score, exit code), NO LLM token. The `/goal` predicate reads the checker output directly; severity maps via the verdict-schema deterministic-checker row, not a VERDICT line. |

Human gates are never auto-sealed. Design approval (`brainstorm`), memory deletion (`memory-prune`, `lt-mem`), upstream mining (`better-super`), and the destructive trio (`session-reaper kill`, `lt-mem` prune, manual `rm`) keep their human decision point; no loop pre-confirms on the human's behalf. The worked reference for the full-binding pattern is `sanity-check/SKILL.md`, section `## Loop integration (converge)`.

## Skill catalogue by category

All 70 skills, grouped by their `category` frontmatter, with the one-line "Use when" purpose.

**delegation** (7)
| Skill | Use when |
|---|---|
| `converge` | converging an artefact through produce-review rounds to a seal |
| `review-dispatch` | resolving the correct adversarial reviewer for an artefact |
| `swarm-dispatch` | launching a parallel w-* batch via W-1/W-4/W-7/W-11 patterns |
| `autocommission` | spawning an ephemeral w-* worker for a one-off task |
| `promote` | promoting a recurring autocommission pattern to a permanent w-* |
| `topology-producer-reviewer` | pairing a producer worker with a reviewer audit dyad |
| `swarm-status` | viewing a live snapshot of in-flight workers and the reviewer queue |

**workflow** (19)
| Skill | Use when |
|---|---|
| `brainstorm` | gating design before implementing a feature or architecture |
| `commit` | creating a conventional commit with auto-detected type |
| `pr` | creating a GitHub pull request with a structured summary |
| `push` | the user explicitly asks to toggle agent git-push permission |
| `fix-issue` | running an end-to-end fix pipeline for a GitHub issue |
| `tdd` | running a RED-GREEN-REFACTOR cycle for features and bugfixes |
| `review` | invoking w-reviewer on staged changes (general/infra/security) |
| `verify` | enforcing evidence-before-claims before writing an RPT |
| `wrap-up` | bundling post-work outcome, mistake, good-idea, and recovery |
| `rb` | refreshing bootstrap.md from state, directive, and pitfalls |
| `notebook` | doing atomic .ipynb edit, execute, or validate work |
| `wf-design` | driving an experimental design to a hostile-review methodology seal |
| `wf-report` | driving a LaTeX report to a sealed finish via /converge SEAL |
| `wf-websearch` | driving multi-agent web research to a saturation seal |
| `wf-wave-monitor` | meta polls orch health on a schedule and seals when all DONE |
| `wf-watchdog` | supervising a converge loop's health and escalating on stall |
| `wf-hpc-watch` | polling a long-running SLURM job, acting only on state change |
| `wf-nb-watch` | watching a long notebook run, dispatching a fix on BROKEN or HUNG |
| `wf-hygiene` | a scheduled hygiene pass checks sessions, memory, checkpoints |

**meta** (9)
| Skill | Use when |
|---|---|
| `sanity-check` | auditing an orch's changes for test-weakening or scope drift |
| `code-quality` | reviewing code quality against programming principles |
| `design-review` | reviewing the design of frontend changes |
| `infra-security` | security-reviewing ~/.claude infrastructure changes |
| `debugging` | applying a systematic debugging methodology |
| `test-infra` | running the infrastructure regression tests |
| `test-scaffold` | generating pytest boilerplate for untested Python modules |
| `orchestrator-patterns` | applying templates or conventions for orchestrated projects |
| `sync-upstream` | syncing the upstream Claude Code reference library and diffing |

**orchestration** (9)
| Skill | Use when |
|---|---|
| `plan` | creating or updating a project plan |
| `status` | showing project status from state files, git status, and plan |
| `handoff` | the user explicitly asks to commission or decommission an ork |
| `delegate` | delegating a task to a fresh subagent with two-stage review |
| `nudge` | probing agent status, self or a named orch |
| `portfolio` | viewing a cross-orch dashboard of orchs, status, and escalations |
| `pleh` | spawning parallel helper agents for self or cross-agent help |
| `session-reaper` | the user explicitly asks to inspect or clean up agent sessions |
| `recover-truncated` | recovering a truncated worker via narrow re-dispatch and handoff |

**memory** (9)
| Skill | Use when |
|---|---|
| `remember` | saving or loading meta context, cheaper than compaction |
| `good-idea` | recording an effective solution or pattern for reuse |
| `mistake` | recording a mistake or promoting a recurring pattern |
| `lt-mem` | consolidating memory DB: re-tier, prune, merge dups |
| `memory-search` | searching agent memory for a keyword or topic |
| `memory-prune` | scanning memory for stale or broken entries (advisory) |
| `mem-index` | browsing the memory DB by tier or type, with stats |
| `mem-similar` | finding memories related or near-duplicate to one |
| `mem-health` | scoring memory DB health across DB-aware criteria |

**domain** (7)
| Skill | Use when |
|---|---|
| `research` | running scientific research, design, literature, review, report |
| `hpc` | generating SLURM scripts, rsync, or parsing job output |
| `experiment` | tracking ML experiments to list, add, or compare metrics |
| `experiment-harness` | running multi-seed experiments and verifying claim provenance |
| `threat-model` | running a STRIDE threat analysis to map and rate attack surface |
| `gas-patterns` | working with Google Apps Script conventions and gotchas |
| `wsl-gotchas` | hitting WSL2 pitfalls in process, filesystem, networking, Git |

**health** (4)
| Skill | Use when |
|---|---|
| `super-health` | scoring superclaude health across its 9 subsystems |
| `health` | checking superclaude infrastructure health |
| `hook-health` | scoring hook subsystem health: syntax, perf, coverage |
| `skill-health` | scoring skill subsystem health: frontmatter, refs, descriptions |

**code-quality** (2)
| Skill | Use when |
|---|---|
| `figure-validate` | validating figures for WCAG-AA contrast, thin lines, low alpha |
| `nb-monitor` | running a notebook with live per-cell progress state |

**research** (1): `hostile-review` (a draft needs an adversarial stress test before commit).
**testing** (1): `test-cleanup-protocol` (cleaning the test env before a run: pycache, compose, git).
**comms** (1): `comms-query` (running ad-hoc SQLite queries against the HCOM broker).
**maintenance** (1): `better-super` (mining and updating superclaude tooling from upstream).

## Composing workflows

Skills build on skills; the convergence machinery is a dependency stack, not a monolith. Two shared SOTs sit at the base: `_shared/verdict-schema.md` (the token protocol, bar levels, no-pre-approval, canonical `/goal` block, severity map) and `_shared/dispatch-contract.md` (the four-part dispatch contract plus the numeric-budget model split). The dependency direction:

```
verdict-schema.md ── tokens, bars, no-pre-approval, /goal shape
dispatch-contract.md ── four-part contract, model split
        │
        ▼
review-dispatch ── resolves artefact class to reviewer + model + rubric
        │
        ▼
converge ── drives produce/review rounds, consumes review-dispatch each round,
            consumes dispatch-contract for the producer, emits the /goal block
        │
        ▼
wf-design · wf-report · wf-websearch · wf-wave-monitor · wf-watchdog ·
wf-hpc-watch · wf-nb-watch · wf-hygiene ── fix converge's slots to one artefact
```

A concrete chain: `/wf-websearch` runs its search waves through `/swarm-dispatch` (the W-7 mixed-batch pattern), audits each synthesis through `/review-dispatch` (which resolves `w-hostile-reviewer` running the `hostile-review` gauntlet), and terminates on the `/goal` block it emits, whose token semantics come from `verdict-schema.md`. Reviewer rubrics are themselves skills (`code-quality`, `design-review`, `hostile-review`, `infra-security`, `sanity-check`), so `/review-dispatch` is a router over the skill fleet, not a fixed table of prompts.

## Cross-references

- Loop engine, iteration protocol, 8 loop rules, ledger, goal-string emission: `~/.claude/skills/converge/SKILL.md`
- Two-token protocol, bar levels, no-pre-approval, canonical `/goal` block, severity map (SOT): `~/.claude/skills/_shared/verdict-schema.md`
- Four-part dispatch contract, numeric budget, model split, reviewer isolation (SOT): `~/.claude/skills/_shared/dispatch-contract.md`
- Reviewer resolution table: `~/.claude/skills/review-dispatch/SKILL.md`
- Worker model x effort matrix, swarm-first mandate: `~/.claude/rules/13-worker-first-mandate.md`
- Swarm quality gates R-1 to R-5 (R-5 is no-pre-approval): `~/.claude/rules/40-swarm-quality-gates.md`
- Entry-point summary and worked examples: `~/.claude/README.md`
