# superclaude

Multi-agent CLI infrastructure for [Claude Code](https://docs.anthropic.com/en/docs/claude-code), running natively on Windows 11 (no WSL, no admin rights). A strategic Meta agent supervising worker swarms, persistent memory, Python lifecycle hooks with guards, and a skill fleet, all from `~/.claude/`.

## Architecture

```
~/.claude/
  agents/        Agent definitions (meta, w-* workers, _ephemeral/, _pending_promotion/)
  hooks/         Python hook dispatcher (dispatch.py) and handlers (sc_hooks/handlers/)
  rules/         Auto-loaded behavioral rules (numbered order)
  skills/        Slash commands (/review, /plan, /tdd, /hostile-review, ...)
  scripts/       Python utilities (memory DB, notebook monitor, figure validator, ...)
  bin/           `mem` and `scpy` shims (work identically in Git Bash and PowerShell)
  comms/         Spawn and outcome logs (_spawns.log, _spawns-rich.log, _outcomes.log)
  agent-memory/  Persistent memory DB
  plans/         Cross-project plans and checkpoints
  logs/          Hook error log (hooks.log)
  tmp/           Scratch files (replaces /tmp)
  docs/          Reference documentation
```

All agents operate from `~/code/`; never `cd` into a project directory; use `git -C <absolute-repo-path>`.

## Hierarchy

Two levels.

| Level | Agent | Role |
|-------|-------|------|
| Strategic | `meta` | Cross-project supervision, plan authoring, swarm dispatch and review. Never edits project code directly |
| Worker | `w-*` (12 permanent, plus ephemeral) | Scoped tasks: implement, review, debug, test, merge, refactor, plan, document, commit |

Workers are spawned through the Agent tool, not launched directly. A plain session with no `--agent` behaves as an ordinary Claude Code session with the rules loaded.

## Install (Windows 11)

**Work in progress.** Installation is done by the scripts under [`docs/win11-migration/`](docs/win11-migration/). In outline:

1. Adopt this repository as `~/.claude` (`C:\Users\<user>\.claude`).
2. Create the dedicated `uv` venv at `~/.claude/.venv` and install the packages listed in `dependencies.yml`.
3. The owner runs `bin/apply-settings.ps1 -Profile superclaude`, which registers the hook dispatcher and the shared permission rules into `settings.json`.

Everything installs at user scope; nothing requires admin rights.

## Usage

```
claude --agent meta          # Strategic planning, swarm supervision
claude                       # Plain session with the rules loaded
```

A `meta` launcher function wraps the first form and sets `CLAUDE_AGENT_NAME=meta`, which is how the hooks recognise the meta session. Details: [`docs/usage-guide.md`](docs/usage-guide.md).

## Key Concepts

| Concept | Location | Purpose |
|---------|----------|---------|
| **Rules** | `rules/` | Auto-loaded behavioral constraints. Numbered for order, path-scoped via frontmatter |
| **Skills** | `skills/` | 50 slash commands and model-invocable protocols. See [Skills](#skills) |
| **Hooks and guards** | `hooks/` | One Python dispatcher per hook event (`dispatch.py <Event>`); handlers aggregate into a single JSON response. Guards: write-acl, content-scan, git-policy, commit-gate, git-verb, heuristics, worker-verify, notebook-guard. See [`docs/guard-subsystem.md`](docs/guard-subsystem.md) |
| **Memory** | `agent-memory/` | Hybrid FTS5 + sqlite-vec SQLite store: shared project gotchas, per-agent instance context, wins. Self-maintains via `/lt-mem` (`mem compact` = FTS-optimize + vec-rebuild + VACUUM) |
| **Memory search** | `mem` (`scripts/memory/memory_db.py`) | Hybrid FTS5 + vector retrieval (query-instruction prefix, widened candidate pool, RRF fusion, multi-query union) with a `--name` resolution ladder that refuses ambiguous guesses. Full protocol, search discipline, and get-ladder detail: `rules/12 § Memory Access` |
| **Spawn logs** | `comms/` | The spawn-log hooks record every worker dispatch and outcome; `/swarm-status` reads them |

## Skills

Skills are the reusable capabilities under `skills/`: 50 of them, each a Markdown file with frontmatter and a slash name. A skill can be invoked two ways: by the human as a slash command (`/plan`, `/review`, `/tdd`), or by a model mid-task through the Skill tool. Both entry points run the same file. The per-skill reference lives in [`skills/README.md`](skills/README.md).

### Review model

Two rules keep review honest. The **two-token protocol**: a producer opens its result with a `STATUS:` line, a reviewer with a `VERDICT:` line (`REWORK` or `CLEAN`, with blocking / major / minor counts); only reviewers author verdicts. The **no-pre-approval rule**: every verdict derives from a fresh examination of the current artefact, and any later change makes a clean verdict stale. Reviews are single-pass: dispatch a reviewer, read the verdict, fix, and re-review only if the owner or meta judges it necessary. `w-hostile-reviewer` is the one-shot adversarial reviewer for methodology, research and reports.

### The wf-* monitors

| Skill | Purpose |
|-------|---------|
| `wf-hpc-watch` | Poll a long-running SLURM job over ssh, act only on a state change |
| `wf-nb-watch` | Watch a long notebook run, dispatch a fix on a BROKEN or HUNG cell |
| `wf-hygiene` | Scheduled advisory pass over memory health and checkpoint staleness |

Each prints a ready-to-paste `/loop` block and stops; the human arms it. A schedule that must outlive the session is a per-user Windows Task Scheduler task running headless `claude -p`; otherwise re-invoke the skill.

```
/wf-hpc-watch 1234567
# prints a /loop (poll the SLURM job) plus a /goal (stop on terminal state); paste both to arm them
```

`/swarm-status` gives a snapshot of in-flight workers, including background reviewers. It mutates nothing and is safe to run at any time.

## Key Commands

| Command | Purpose |
|--------|---------|
| `mem` | CLI over the memory DB: `mem search "..." [-k N]`, `mem get "..."`, `mem similar "..."`, `mem list [--tier T]`, `mem stats`, `mem compact` |
| `scpy <script.py> [args]` | Run a superclaude script (path relative to `~/.claude/scripts/`) under the `~/.claude/.venv` interpreter |
| `scpy memory/mem_health.py` | Memory DB health score (DB-aware: row size, FTS index cohesion, embedding coverage) |
| `scpy better_super_deps.py <flags>` | Manage the dependency manifest: `--list`, `--check`, `--record`, `--export --out <file>` |

## Configuration

`settings.json` controls permissions (`allow`/`deny` command lists), environment variables, and hook registration. It is written by `bin/apply-settings.ps1 -Profile superclaude`, run by the owner. There is no sandbox on native Windows: enforcement is the write-ACL guard plus `permissions.deny`.

Guard kill switches: `SUPERCLAUDE_GUARDS=off` disables every guard; `SUPERCLAUDE_GUARD_<NAME>=off|warn|block` sets one.

## Dependencies

Single source of truth: [`dependencies.yml`](dependencies.yml). Python packages are installed into a dedicated venv at `~/.claude/.venv` (kept separate from project-local venvs), managed with `uv`:

```
uv venv ~/.claude/.venv --python 3.12
uv pip install --python ~/.claude/.venv/Scripts/python.exe <package>
```

| Dependency | Type | Used by | Source |
|-----------|------|---------|--------|
| fastembed | Python (venv) | memory embeddings | `dependencies.yml` |
| sqlite-vec | Python (venv) | memory vector search | `dependencies.yml` |
| nbclient, nbformat | Python (venv) | notebook runtime monitor | `dependencies.yml` |
| Pillow, numpy | Python (venv) | figure pre-flight validator | `dependencies.yml` |
| SQLite + FTS5 | Python standard library (`sqlite3` module) | memory keyword search | bundled with Python |
| latexmk, pdflatex, standalone, quantikz, pgfplots, dvisvgm | MiKTeX (user scope) | TikZ memory/report rendering | MiKTeX |
| mermaid.js, viz.js (graphviz), vega-embed | client-side JS (CDN/bundled in viewer) | Mermaid / DOT / chart rendering in the HTML viewer | none — runs in-browser |
| node | `fnm` (user scope) | tooling / statusline | — |
| git, gh, uv, OpenSSH client | user scope / Windows built-in | version control, GitHub, venv, HPC access | — |

To obtain a requirements-format file: `scpy better_super_deps.py --export --out ~/.claude/tmp/req.txt`

Superclaude scripts run through the `scpy` / `mem` shims, never through shell activation, so superclaude tooling never shadows project-local venvs. The hook dispatcher deliberately uses the base interpreter and the standard library only, to stay fast.

## License

MIT
