# Win11 migration: text-pass conventions

Single source of truth for every agent editing prompt assets (rules, agents, skills, docs) on the `win11` branch.
Target: Claude Code running natively on Windows 11 (PowerShell 7 / 5.1, Git Bash available), no WSL, no admin rights.

## 0. Ground rules

- Preserve the substance. This framework's value is its scientific software-engineering discipline (read before edit, minimal diffs, verification, worker-first delegation, memory discipline). Do not water it down, summarise it away, or "improve" it. Change only what the conventions below require.
- Minimal diffs. Do not reflow paragraphs, renumber sections, or restyle. `rules/06-writing-style.md` applies: no em or en dashes in anything you write.
- Edit only the files assigned to you. Do not commit. Do not touch `hooks/`, `scripts/`, `bin/`, `settings.json.example`, or `skills/notebook/`.
- If a whole section exists only for a dropped subsystem, delete the section. If a sentence merely mentions one, remove the mention and keep the sentence coherent.
- When unsure whether something is in scope, leave it and list it in your report under "Ambiguous".

## 1. Dropped subsystems (remove all references)

| Subsystem | Tell-tale terms |
|---|---|
| Remote server and peer sync | toto, peer, `tsh`, `tsudo`, `skm`, cockpit, `claude_mem_sync.py`, tailscale, ntfy, "two-machine", "peer-synced", `-conflict-<peer>` |
| Orch tier | `orch`, `orch-<name>`, `o-<proj>-<seq>`, ork, "Tactical", handoff, commission/decommission, DIR-NNN, RPT-NNN, ESC-NNN, `directives.md`, `bootstrap.md`, `reports.md`, `escalations.md`, `meta-registry.md`, `parent.session` |
| Scaf agent | scaf, Scaffolder |
| Comms broker (HCOM) | hcom, `.broker.db`, `.comms.db`, `comms_db.py`, `comms_viewer.py`, `hcom-broker.py`, any `sqlite3 ~/.claude/comms/...` command, NUDGE, "dual-write" |
| Converge and looping protocols | `/converge`, `/wf-auto`, `/wf-design`, `/wf-report`, `/wf-websearch`, `/wf-watchdog`, `/wf-wave-monitor`, `/review-dispatch`, `/topology-producer-reviewer`, `/swarm-observe`, `converge_auto.py`, `bin/loops`, SEAL, seal manifest, `rounds.md`, round ledger, `Ledger:` line, R-5, guards 60/62/64/70, `seal-manifest.py`, `instrument-tripwire.py` |
| Session timer | 45/48/53-minute limits, `session-timers/*.start`, `.override`, edit/call counters, `/nudge`, `/session-reaper`, `/rb` |
| Bash health suites | `/health`, `/hook-health`, `/skill-health`, `/test-infra`, `super-health.sh`, `infra-test.sh`, `test-hooks.sh`. Note: `/super-health` is re-implemented in Python (`scripts/health/super_health.py`). |
| WSL specifics | `/mnt/c`, WSL file-mode (755 to 644) diffs, `core.fileMode` stash dance, `sg docker -c`, `/wsl-gotchas` (replaced by `/windows-gotchas`) |
| Docker services | crawl4ai, firecrawl, `better-super --web` |

Deleted skills (remove from any list, catalogue, or cross-reference): comms-query, converge, delegate, handoff, health, hook-health, nudge, portfolio, rb, review-dispatch, session-reaper, skill-health, swarm-observe, test-infra, topology-producer-reviewer, wf-auto, wf-design, wf-report, wf-watchdog, wf-wave-monitor, wf-websearch, wsl-gotchas.

Deleted agents: orch, scaf.

### What survives of the review machinery

- The hierarchy is now two levels: **Meta** (strategic, supervises swarms, never edits project code directly... unchanged) and **Workers** (`w-*` plus ephemeral). A plain session with no `--agent` behaves as an ordinary Claude Code session with the rules loaded.
- First-line result tokens survive in reduced form: producers open with `STATUS:`, reviewers with `VERDICT:`. There is no `SEAL:` token, no revision binding, no round ledger.
- `w-hostile-reviewer` survives as a one-shot adversarial reviewer that returns a `VERDICT:`. It does not seal and does not loop.
- Rules R-1 to R-4 in `rules/40` survive. R-5 is removed.
- Reviews are single-pass: dispatch a reviewer, read the verdict, fix, and re-review only if the owner or meta judges it necessary. No mandated iteration protocol.

## 2. Paths

| Old | New |
|---|---|
| `~/projects/cash/` and `~/projects/` (workspace root, agent CWD) | `~/code/` |
| `$HOME/projects/cash/<x>` | `~/code/<x>` |
| `/home/totob/...` | `~/...` |
| `/tmp/...` | `~/.claude/tmp/...` |
| `~/.claude/...` | unchanged. `~` resolves to `C:\Users\<user>` in both Git Bash and the owner's PowerShell profile. |

Keep forward slashes in prose and commands. The canonical workspace statement becomes: "all agents operate from `~/code/`; never `cd` into a project directory; use `git -C <absolute-repo-path>`".

## 3. Commands

Agents on this machine can run commands through either the Bash tool (Git Bash) or the PowerShell tool. Prompt text must therefore avoid shell-specific syntax. Use these shims, which work identically in both shells and are on PATH:

| Old | New |
|---|---|
| `HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py <sub> ...` (any spelling) | `mem <sub> ...` |
| `~/.claude/bin/mem ...` | `mem ...` |
| `~/.claude/.venv/bin/python ~/.claude/scripts/<path>.py ...` | `scpy <path>.py ...` (path relative to `~/.claude/scripts/`) |
| `python3 ...` for a superclaude script | `scpy ...` |
| heredoc bodies (`--text-stdin <<'EOF'`) | write the body to `~/.claude/tmp/<name>.md` with the Write tool, then pass `--text-file ~/.claude/tmp/<name>.md` |
| `VAR=value command` env prefixes | drop the prefix. `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE` are set globally in `settings.json`. |
| `mkdir -p`, `touch`, `printf > file` | use the Write tool to create the file (parents are created automatically) |
| `sqlite3 <memory db> "<sql>"` | `mem list`, `mem stats`, or `mem search`. If raw SQL is truly needed: `scpy memory/memory_db.py sql "<query>"` and list it in your report so the subcommand gets implemented. |
| `jq` | ask for the script's `--json` output and read it, or use `scpy` with a Python one-liner script |
| `timeout N cmd` | drop `timeout`; state the expected duration and use the tool's own timeout parameter |
| cron, crontab, `nohup`, `tmux`, `systemctl` | "a per-user Windows Task Scheduler task" where a scheduler is genuinely required; otherwise remove |
| `pgrep`, `ps aux`, `kill -0`, `/proc/...` | remove, or "check with `Get-Process`" if the step is essential |
| `find ... -exec rm -rf` | describe the target precisely and instruct deletion of the named paths; no recursive wildcard deletes |
| `bash -n`, `shellcheck`, `chmod +x` | remove. Hooks on this instance are Python; validate with `scpy -m py_compile <file>` only if a validation step is essential. |

`mem` exposes the whole `memory_db.py` CLI: `init`, `search`, `upsert`, `get`, `list`, `prune`, `stats`, `compact`, `similar`, `archive`, `retier`, `export`.

`gh`, `git`, `latexmk`, `uv`, `python`, `R`, `Rscript` are installed and may be referenced normally.

## 4. Hooks referenced by name

Hooks are being rewritten in Python as one dispatcher per event. Refer to behaviour, not to bash filenames:

| Old reference | New wording |
|---|---|
| `hooks/guards/26-git-policy.sh` | "the git-policy guard" |
| `hooks/guards/20-write-acl.sh` | "the write-ACL guard" |
| `hooks/guards/10-content-scan.sh` | "the content-scan guard" |
| `hooks/guards/30-commit-gate.sh`, `modules/25-commit-gate.sh` | "the commit-gate guard" |
| `hooks/guards/80-worker-verify.sh` | "the worker-verify reminder" |
| `hooks/modules/15-baseline-stash.sh` | "the baseline-stash hook" |
| `hooks/modules/30-notebook-guard.sh` | "the notebook guard" |
| `hooks/modules/45-spawn-log.sh`, `agent-outcome.sh`, `subagent-stop.sh` | "the spawn-log hooks" (logs stay at `~/.claude/comms/_spawns.log`, `_spawns-rich.log`, `_outcomes.log`) |
| `memory-inject.sh` | "the SessionStart memory-injection hook" |
| `pre-compact.sh`, `stop.sh` | "the pre-compact snapshot hook", "the stop snapshot hook" |
| `05-context-check.sh` | "the context-check hook" |

Claims that the sandbox enforces something must go: there is no sandbox on native Windows. Enforcement is the write-ACL guard plus `permissions.deny`.

## 5. Language policy (new, applies to every agent)

- Reason and communicate in English at all times. When unsure, default to English.
- Italian is required for: formal deliverables and documents, and comments inside code (never identifiers or the code itself).

Only the agent assigned `rules/` adds this as a rule file. Others should not restate it.

## 6. Report format

Finish with a report containing: files changed (with a one-line note each), sections deleted, "Ambiguous" items you left alone, and any `scpy`/`mem` subcommand you referenced that may not exist yet.
