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
| **Skills** | `skills/` | 30 slash commands — user-invocable or preloaded into agents |
| **Comms** | `comms/` | Structured message bus: directives (meta->orch), reports (orch->meta), escalations |
| **Hooks** | `hooks/` | Session timer (35/40/48 min), pre-compaction snapshots, cleanup |
| **Memory** | `agent-memory/` | Hybrid FTS5 + sqlite-vec SQLite store — shared project gotchas, per-agent instance context, wins. Self-maintains via `/lt-mem` (`memory_db.py compact` = FTS-optimize + vec-rebuild + VACUUM) |

## Key Scripts

| Script | Purpose |
|--------|---------|
| `scripts/session-reaper.sh` | Kill zombie claude processes and clean stale session timer files. Safe to run anytime; `--dry-run` to preview, `--all` to also reap long-running active sessions |
| `scripts/super-health.sh` | Weighted health score (0–100, letter grade) across 7 infrastructure components. `--quick` / `--standard` / `--deep` / `--complete` tiers |
| `scripts/mem-health.sh` | Memory DB health score (6 criteria, 100 pts). DB-aware: measures row size, FTS index cohesion, and embedding coverage |
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
