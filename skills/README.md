# Superclaude Skill System: Reference

The skills under `~/.claude/skills/*/SKILL.md` (50 of them) are the invocable capability layer of superclaude. Each skill is a self-contained protocol, a slash command a user types (`/plan`) and, by default, a capability the model can select autonomously through the Skill tool. This file is the mechanical reference. The entry-point summary lives in the global `~/.claude/README.md`; this file does not repeat it. It owns the mechanics: the frontmatter contract, the two-token protocol, single-pass review, the scheduled wf-* monitors, and the catalogue.

## Skill anatomy

A skill is a Markdown file with YAML frontmatter followed by the protocol body. The frontmatter is the contract between the skill and the two things that invoke it (a human via slash, the model via the Skill tool).

| Key | Meaning |
|---|---|
| `name` | The slash command and Skill-tool name (`/plan`). |
| `description` | A `"Use when ..."` trigger, kept under ~80 characters. This string is what the model matches against to auto-select the skill, so it is written as a triggering condition, not a title. |
| `category` | Taxonomy bucket (delegation, workflow, meta, memory, orchestration, domain, code-quality, research, testing, maintenance). Drives the catalogue below. |
| `user-invocable` | `true` (the default) makes the skill a slash command. Set `false` for model-only protocols the user should not fire directly (7 skills: `debugging`, `code-quality`, `verify`, `infra-security`, `gas-patterns`, `windows-gotchas`, `test-cleanup-protocol`). |
| `disable-model-invocation` | Absent (the default) means the skill is model-invocable. Setting it `true` suppresses that; no skill sets it `true`. One skill, `notebook`, still carries the key set explicitly to `false`, a redundant restatement of the default. |
| `allowed-tools` | The tool grant for ONE invocation of the skill. |
| `argument-hint` | The usage arguments shown to the user. |
| `context: fork` + `agent: <w-*>` | Marks a skill that forks its work to a named subagent: `plan` to `w-planner`, `sanity-check` and `review` to `w-reviewer`. |

Invocation has two paths that resolve to the same protocol: a user types `/name args` (slash), or the model emits a Skill-tool call `Skill(name)`. Rubrics are read by path; an agent may preload one via its own agent-definition `skills:` frontmatter (for example `w-hostile-reviewer` preloads the `hostile-review` gauntlet, `w-debugger` preloads `debugging` and `windows-gotchas`), which is an agent-def key, not a skill-file key.

## Two-token protocol and single-pass review

The **two-token protocol** (SOT: `_shared/verdict-schema.md`) is the spine. Two line-1 tokens, each the first line of an agent's final message so it survives truncation:

- `VERDICT: REWORK|CLEAN blocking=N major=N minor=N` from a reviewer.
- `STATUS: DONE|PARTIAL|FAILED files=N checkpoint=<path>` from a non-reviewer producer.

Only reviewer subagents author VERDICT; producers and the parent never do. The parent QUOTES a reviewer token verbatim.

Reviews are **single-pass**: dispatch a reviewer, read the verdict, fix the findings, and re-review only if the owner or meta judges it necessary. There is no mandated iteration protocol. The standing review disciplines still apply to every dispatch:

- **Rubric-bound reviewers.** Every dispatch carries an explicit rubric; never "review this".
- **Reviewer isolation.** The reviewer sees artefact, diff, and rubric only, never the producer's reasoning or a prior clean verdict.
- **Tool-verified critique.** Reviewers RUN the tests, linters, and compile gates themselves; a producer self-report is never evidence of record.
- **Anti-hacking sweep.** An automatic blocking finding in any scope (weakened assertions, harness escapes, skipped coverage, diagnostic theatre).
- **No pre-approval.** Every VERDICT derives from a fresh, explicit examination of the CURRENT state of the work. Approval never transfers: a re-review may not rest on "my prior findings were addressed" without re-examining the current artefact and citing fresh `file:line` evidence. Any change to the artefact after a `VERDICT: CLEAN` makes that verdict stale.

## Reviewer selection

Which reviewer fits which artefact (each dispatch carries the four-part contract in `_shared/dispatch-contract.md`, reviewer isolation, the verdict contract quoted verbatim, and a numeric budget with the model split):

| Artefact class | Reviewer | Model | Rubric |
|---|---|---|---|
| code, small (<=3 files) | w-reviewer | sonnet | `code-quality` |
| code, large / architectural | w-reviewer | opus | `code-quality` |
| frontend | w-design-reviewer | sonnet (opus cross-page) | `design-review` |
| methodology / research / reports | w-hostile-reviewer | opus | `hostile-review` |
| infra (`~/.claude` changes) | w-reviewer | sonnet / opus by scale | `infra-security` |
| figures | figure-validate (no LLM, deterministic) | n/a | `figure-validate` |
| test integrity, scope drift | w-reviewer | sonnet | `sanity-check` (5-category) |

`sanity-check` audits a worker's or swarm's changes: `/sanity-check <repo-path> <branch-or-commit-range> [task-brief-path]`. `w-hostile-reviewer` is a one-shot adversarial reviewer: it returns a `VERDICT:` and does not loop.

## The wf-* family

Three scheduled monitors. Each PRINTS its ready-to-paste `/loop` block (the two poll-then-act monitors also print a `/goal` stop condition) and stops; none self-arms. `/loop` is the harness time engine: a recurring scheduler that re-runs a tick on an interval.

| Skill | Kind | Purpose | Invocation |
|---|---|---|---|
| `wf-hpc-watch` | poll-then-act | Poll a long-running SLURM job read-only over ssh; act only on a RUNNING to DONE or FAILED transition; stop on a fresh terminal state (plus an optional one-shot output audit). | `/wf-hpc-watch <job-id|job-name> [--interval 15m]` |
| `wf-nb-watch` | poll-then-act | Watch a notebook run through `nb-monitor`'s progress file; on BROKEN or HUNG dispatch a fix and re-run; stop on a clean completion plus a fresh reviewer verdict. | `/wf-nb-watch <notebook-path> [--interval 10m]` |
| `wf-hygiene` | gardener | Scheduled read-only pass over memory-DB score (`scpy memory/mem_health.py --quick`) and checkpoint staleness. No artefact, no verdict; flags for the human only. | `/wf-hygiene [--interval daily]` |

Durability: `/loop` schedules die with the session. A run that must outlive the session uses a per-user Windows Task Scheduler task running headless `claude -p`; otherwise simply re-invoke the skill when a session is live (the monitors keep their tick state on disk under `~/.claude/`).

## Skill nature: reviewer, one-shot, deterministic gate

A skill gets exactly the review treatment its nature warrants; forcing ceremony onto a one-shot skill is diagnostic theatre.

| Nature | Applies to | Pattern |
|---|---|---|
| Reviewer rubric | `review`, `design-review`, `sanity-check`, `code-quality`, `hostile-review`, `infra-security` | Run by a reviewer subagent, single-pass, returns a `VERDICT:` line with the severity mapping in `verdict-schema.md`. |
| One-shot with self-check | retrospectives, advisory scans (`wrap-up`, `mistake`, `good-idea`, `verify`, `memory-search`) | Runs once and self-checks its own output. |
| Deterministic gate | computed-result checkers (`figure-validate`) | Gates on a computed value (flag count), NO LLM token. Severity maps via the verdict-schema deterministic-checker row, not a VERDICT line. |

Human gates are never auto-cleared. Design approval (`brainstorm`), memory deletion (`memory-prune`, `lt-mem`), upstream mining (`better-super`), and any manual file deletion keep their human decision point; no skill pre-confirms on the human's behalf.

## Skill catalogue by category

All 50 skills, grouped by their `category` frontmatter, with the one-line "Use when" purpose.

**delegation** (4)
| Skill | Use when |
|---|---|
| `swarm-dispatch` | launching a parallel w-* batch via W-1/W-4/W-7/W-11 patterns |
| `autocommission` | spawning an ephemeral w-* worker for a one-off task |
| `promote` | promoting a recurring autocommission pattern to a permanent w-* |
| `swarm-status` | viewing a live snapshot of in-flight workers and reviewer queue |

**workflow** (13)
| Skill | Use when |
|---|---|
| `brainstorm` | gating design before implementing a feature or architecture |
| `commit` | drafting a conventional commit message (read-only, does not commit) |
| `pr` | creating a GitHub pull request with a structured summary |
| `git` | the user explicitly toggles agent git commit and push permission |
| `fix-issue` | running an end-to-end fix pipeline for a GitHub issue |
| `tdd` | running a RED-GREEN-REFACTOR cycle for features and bugfixes |
| `review` | invoking w-reviewer on staged changes, general/infra/security |
| `verify` | enforcing evidence-before-claims before reporting completion |
| `wrap-up` | bundling post-work outcome, mistake, good-idea, and recovery |
| `notebook` | doing atomic .ipynb edit, execute, or validate work |
| `wf-hpc-watch` | polling a long-running SLURM job, acting only on state change |
| `wf-nb-watch` | watching a long notebook run, dispatching a fix on BROKEN or HUNG |
| `wf-hygiene` | a scheduled hygiene pass checks memory health and checkpoints |

**meta** (8)
| Skill | Use when |
|---|---|
| `sanity-check` | auditing a worker's or swarm's changes for test-weakening or scope drift |
| `code-quality` | reviewing code quality against programming principles |
| `design-review` | reviewing the design of frontend changes |
| `infra-security` | security-reviewing ~/.claude infrastructure changes |
| `debugging` | applying a systematic debugging methodology |
| `test-scaffold` | generating pytest boilerplate for untested Python modules |
| `orchestrator-patterns` | applying templates or conventions for orchestrated projects |
| `sync-upstream` | syncing the upstream Claude Code reference library and diffing |

**orchestration** (4)
| Skill | Use when |
|---|---|
| `plan` | creating or updating a project plan |
| `status` | showing project status from state files, git status and plan |
| `pleh` | spawning parallel helper agents for self or cross-agent help |
| `recover-truncated` | recovering a truncated worker via narrow re-dispatch |

**memory** (9)
| Skill | Use when |
|---|---|
| `remember` | saving or loading meta context, cheaper than compaction |
| `good-idea` | recording an effective solution or pattern for reuse |
| `mistake` | recording a mistake or promoting a recurring pattern |
| `lt-mem` | consolidating memory DB; re-tier, prune, merge dups |
| `memory-search` | searching agent memory for a keyword or topic |
| `memory-prune` | scanning memory for stale or broken entries; advisory |
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
| `windows-gotchas` | a failure may stem from native Windows: shells, paths, Python, locks |

**code-quality** (2)
| Skill | Use when |
|---|---|
| `figure-validate` | validating figures for WCAG-AA contrast, thin lines, low alpha |
| `nb-monitor` | running a notebook with live per-cell progress state |

**research** (1): `hostile-review` (a draft needs an adversarial stress test before commit).
**testing** (1): `test-cleanup-protocol` (cleaning test env before a run; clears pycache, dirtied files, git).
**maintenance** (1): `better-super` (mining and updating superclaude tooling from upstream).

## Composing workflows

Skills build on skills. Two shared SOTs sit at the base: `_shared/verdict-schema.md` (the token protocol, no-pre-approval, severity map) and `_shared/dispatch-contract.md` (the four-part dispatch contract plus the numeric-budget model split). The dependency direction:

```
verdict-schema.md ── tokens, no-pre-approval, severity map
dispatch-contract.md ── four-part contract, model split
        │
        ▼
review · design-review · hostile-review · sanity-check ── one-shot reviewer
    dispatches returning a VERDICT line
        │
        ▼
swarm-dispatch · autocommission · pleh ── producers (STATUS line), audited by
    the reviewers above
        │
        ▼
wf-hpc-watch · wf-nb-watch ── scheduled monitors that dispatch a fix worker or
    a one-shot reviewer only on a state transition
wf-hygiene ── scheduled gardener pass; no artefact, flags for the human only
```

Reviewer rubrics are themselves skills (`code-quality`, `design-review`, `hostile-review`, `infra-security`, `sanity-check`), so reviewer selection is a choice over the skill fleet, not a fixed table of prompts.

## Cross-references

- Two-token protocol, no-pre-approval, severity map (SOT): `~/.claude/skills/_shared/verdict-schema.md`
- Four-part dispatch contract, numeric budget, model split, reviewer isolation (SOT): `~/.claude/skills/_shared/dispatch-contract.md`
- Worker model x effort matrix, swarm-first mandate: `~/.claude/rules/13-worker-first-mandate.md`
- Swarm quality gates R-1 to R-4: `~/.claude/rules/40-swarm-quality-gates.md`
- Entry-point summary: `~/.claude/README.md`
