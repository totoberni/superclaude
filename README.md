# superclaude

Multi-agent CLI infrastructure for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Hierarchical agents, persistent memory, lifecycle hooks, and structured orchestration — all from `~/.claude/`.

## Architecture

```
~/.claude/
  agents/        Agent definitions (meta, scaf, orch, workers)
  hooks/         Lifecycle hooks (session timer, compaction, cleanup)
  rules/         Auto-loaded behavioral rules (numbered order)
  skills/        Slash commands (/review, /plan, /tdd, /health, ...)
  scripts/       Utility scripts (session reaper, infra health)
  comms/         Meta <> Orch communication bus
  agent-memory/  Persistent memory (shared + per-agent)
  plans/         Cross-project orchestration plans
  docs/          Reference documentation
```

## Hierarchy

| Level | Agent | Model | Role |
|-------|-------|-------|------|
| Strategic | `meta` | opus | Cross-project supervision, directives, plan authoring |
| Infrastructure | `scaf` | opus | `~/.claude/` specialist: agents, hooks, rules, skills, settings |
| Tactical | `orch` | opus | Project execution, worker delegation, git, code editing |
| Worker | `w-*` | opus | Scoped tasks: review, debug, merge, refactor, plan |

Workers are spawned by orchs via the Agent tool — not launched directly.

## Setup

```bash
git clone https://github.com/<YOUR_USERNAME>/superclaude.git ~/.claude/

# Customize for your environment
$EDITOR ~/.claude/CLAUDE.md       # Your profile and project inventory
$EDITOR ~/.claude/settings.json   # Permissions, sandbox, allowed commands
```

## Usage

```bash
claude --agent meta          # Strategic planning, orch supervision
claude --agent scaf          # Infrastructure edits (settings, hooks, rules)
claude --agent orch          # Direct project work
claude --agent o-<name>      # Named orch instance (project-specific thin alias)
```

## Key Concepts

| Concept | Location | Purpose |
|---------|----------|---------|
| **Rules** | `rules/` | Auto-loaded behavioral constraints. Numbered for order, path-scoped via frontmatter |
| **Skills** | `skills/` | ~70 slash commands and workflow skills: user-invocable or model-invocable, every one loop-composable. See [Skills and Workflow Skills](#skills-and-workflow-skills) |
| **Comms** | `comms/` | Structured message bus: directives (meta->orch), reports (orch->meta), escalations |
| **Hooks** | `hooks/` | Session timer (35/40/48 min), pre-compaction snapshots, cleanup |
| **Memory** | `agent-memory/` | Hybrid FTS5 + sqlite-vec SQLite store: shared project gotchas, per-agent instance context, wins. Self-maintains via `/lt-mem` (`memory_db.py compact` = FTS-optimize + vec-rebuild + VACUUM) |
| **Memory search** | `scripts/memory/memory_db.py` | Hybrid FTS5 + vector retrieval (query-instruction prefix, widened candidate pool, RRF fusion, multi-query union) with a `--name` resolution ladder that refuses ambiguous guesses. Full protocol, search discipline, and get-ladder detail: `rules/12 § Memory Access` |
| **Two-machine git model** | git (`main`) | Single shared `main`, one machine is SOT and sole pusher, the peer is a pull-only mirror (`git fetch && git reset --hard origin/main`). Machine-local state (`plans/`, `agent-memory/`, live `bin/` wrappers) is gitignored and travels via sync tools, not git |

## Skills and Workflow Skills

Skills are the reusable capabilities under `skills/`: roughly 70 of them, each a Markdown file with frontmatter and a slash name. A skill can be invoked two ways: by the human as a slash command (`/plan`, `/review`, `/tdd`, `/converge`), or by a model mid-task through the Skill tool. Both entry points run the same file. Every skill is now loop-composable: the `disable-model-invocation` flag defaults to false, so any skill can be model-invoked and driven inside a recurring loop, not just typed once by a human. The deep per-skill reference lives in [`skills/README.md`](skills/README.md); this section is the summative entry point plus worked examples.

### The workflow-skill model

A workflow skill converges an artefact: it produces, reviews, and iterates until an independent auditor seals the result. Two engine skills underpin the family:

- **`/converge`** runs the produce-review loop. Each round a producer worker builds or revises the artefact, then a rubric-bound reviewer audits it, and the loop repeats to a sealed finish.
- **`/review-dispatch`** resolves the correct adversarial reviewer for an artefact class (a LaTeX report routes to `w-hostile-reviewer`, a frontend diff to `w-design-reviewer`), preloading the right rubric and effort.

Two rules keep the loop honest. The **two-token protocol**: every round a reviewer emits one `VERDICT` line (`REWORK` or `CLEAN`, with blocking / major / minor counts); termination comes only from a `SEAL` line emitted by a FRESH auditor that examined the complete final state, never the round reviewer. The **no-pre-approval rule**: a seal binds to a named artefact revision, and any later change to the artefact voids it and forces a fresh seal, so approval never transfers across rounds. Full mechanics (bar levels, conductor context, the round ledger) are documented in [`skills/README.md`](skills/README.md).

### The wf-* family

Each `wf-*` skill is a thin binding that fixes `/converge`'s slots to one domain, then prints a ready-to-paste goal block. Three flagship bindings drive an artefact to a seal; five schedule bindings poll a signal on an interval and act only on a change.

| Skill | Kind | Purpose |
|-------|------|---------|
| `wf-design` | flagship | Drive an experimental design to a hostile-review methodology seal (all 13 design steps) |
| `wf-report` | flagship | Drive a LaTeX report to a sealed finish: dual gate of a clean `latexmk` compile plus a fresh hostile SEAL |
| `wf-websearch` | flagship | Drive multi-agent web research to a saturation seal (a wave that surfaces no new must-read source) |
| `wf-wave-monitor` | schedule | Meta polls orch health on an interval, seals when all orks report DONE with zero open escalations |
| `wf-watchdog` | schedule | Supervise a converge loop's health, escalate on stall or oscillation |
| `wf-hpc-watch` | schedule | Poll a long-running SLURM job, act only on a state change |
| `wf-nb-watch` | schedule | Watch a long notebook run, dispatch a fix on a BROKEN or HUNG cell |
| `wf-hygiene` | schedule | Scheduled hygiene pass over sessions, memory, and checkpoints |

### Usage examples

Every `wf-*` skill follows a print-then-paste flow: the skill configures the loop and PRINTS a `/goal` block (schedule skills print a `/loop` block too), then STOPS. The human pastes the block to arm the external judge. The skill never arms the engine itself, which keeps the judge independent of the producer.

Drive a LaTeX report to a sealed finish:

```
/wf-report path/to/report.tex
# then paste the printed /goal block to arm the seal;
# the loop iterates w-doc against w-hostile-reviewer until a fresh SEAL over a clean compile
```

Run iterative web research to saturation:

```
/wf-websearch "what is the current state of retrieval-augmented generation"
# waves of up to 5 parallel searchers; seals when a wave adds no new source and the synthesis passes a clean hostile SEAL
```

Design an experiment to a methodology seal:

```
/wf-design "why does model M show behaviour B under distribution shift"
# a producer drafts against the 13 design steps; w-hostile-reviewer audits methodology each round
```

Converge any artefact generically:

```
/converge path/to/artefact
# prints the /goal block; paste it, and the produce-review loop runs to a fresh SEAL
```

Schedule a monitor over a long job:

```
/wf-hpc-watch 1234567
# prints a /loop (poll the SLURM job) plus a /goal (seal on terminal state); paste both to arm them
```

## Key Scripts

| Script | Purpose |
|--------|---------|
| `scripts/session-reaper.sh` | Kill zombie claude processes and clean stale session timer files. Safe to run anytime; `--dry-run` to preview, `--all` to also reap long-running active sessions |
| `scripts/super-health.sh` | Weighted health score (0-100, letter grade) across 7 infrastructure components. `--quick` / `--standard` / `--deep` / `--complete` tiers. `--deep` also asserts the memory get-ladder, ambiguous-name refusal, and live hybrid top-k |
| `scripts/mem-health.sh` | Memory DB health score (6 criteria, 100 pts). DB-aware: measures row size, FTS index cohesion, and embedding coverage |
| `bin/mem` | Shorthand CLI over `memory_db.py`: `mem search "..." [-k N]`, `mem get "..."`, `mem similar "..."`, `mem list [--tier T]`. Drops the long invocation and the `HF_HUB_OFFLINE=1` prefix |
| `bin/tsudo`, `bin/tsh` | Peer-machine root/exec shortcuts over an ssh-agent-keyed channel (`pam_ssh_agent_auth`). Live wrappers are machine-specific and gitignored; adopt via the `bin/*.example` templates (same convention as `scripts/cockpit/*.example`), replacing the `PEERHOST`/`PEERNAME`/`KEYFILE` placeholders |
| `scripts/better-super-deps.sh` | Manage the `.venv` dependency set: `--pip-install` to refresh, `--export --out <file>` to dump a requirements file |
| `scripts/cockpit/cockpit.{bash,fish}.example` | Optional shell shortcuts (templates): reconcile memory with a peer host, then open SSH. Copy to `cockpit.{bash,fish}` (gitignored), replace `PEER` with your SSH host alias, and source from your shell rc |

## Configuration

`settings.json` controls permissions (`allow`/`deny` command lists), sandbox (filesystem + network), and hook registration. Only `scaf` may edit it.

Details: [`docs/usage-guide.md`](docs/usage-guide.md) | Comms protocol: [`comms/README.md`](comms/README.md)

## Dependencies

Single source of truth: [`dependencies.yml`](dependencies.yml). Python packages are installed into a dedicated venv at `~/.claude/.venv` (kept separate from project-local venvs).

| Dependency | Type | Used by | Source |
|-----------|------|---------|--------|
| fastembed | Python (venv) | memory embeddings | `dependencies.yml` |
| sqlite-vec | Python (venv) | memory vector search | `dependencies.yml` |
| nbclient, nbformat | Python (venv) | notebook runtime monitor | `dependencies.yml` |
| Pillow, numpy | Python (venv) | figure pre-flight validator | `dependencies.yml` |
| crawl4ai | Docker-only | `/better-super --web` (Phase 6 docker service) | `dependencies.yml` (docker section) |
| sqlite3 + FTS5 | system (present) | memory keyword search | bundled with OS |
| latexmk, pdflatex, standalone, quantikz, pgfplots, dvisvgm | system TeX Live (present) | TikZ memory/report rendering | TeX Live |
| mermaid.js, viz.js (graphviz), vega-embed | client-side JS (CDN/bundled in viewer) | Mermaid / DOT / chart rendering in the HTML viewer | none — runs in-browser |
| docker | system (present) | Crawl4AI container (Phase 6) | — |
| node | system (present) | tooling / statusline | — |

Setup / refresh the venv:
```bash
python3 -m venv ~/.claude/.venv
~/.claude/scripts/better-super-deps.sh --pip-install
```

To obtain a requirements-format file: `~/.claude/scripts/better-super-deps.sh --export --out /tmp/req.txt`

Superclaude scripts invoke `~/.claude/.venv/bin/python` by **absolute path** — no shell activation required (so superclaude tooling never shadows project-local venvs).

## HCOM (Hook Comms — Phase A scaffolded)

SQLite-backed message broker + mid-turn injection hook. Scaffolded but not yet migrated. See [`docs/hcom-design.md`](docs/hcom-design.md) for the full plan. Status: opt-in (silent if `~/.claude/comms/.broker.db` not initialized via `~/.claude/scripts/hcom-init.sh`). Phase B (dual-write) deferred to next session per DEC-006.

## License

MIT
