# _shared/dispatch-contract.md: worker dispatch contract v2 (SOT)

Consumed by: /swarm-dispatch, /autocommission, meta dispatch prompts. Extends rules/40 gates; verdict tokens per [_shared/verdict-schema.md].

Every Agent-tool spawn prompt from meta (or a plain session) carries the four parts (Anthropic multi-agent doctrine) plus three superclaude extensions.

## Four parts

1. **Objective**: one unambiguous goal, stated first.
2. **Output format**: expected artifact(s) + the verdict-schema first line (STATUS for producers, VERDICT for reviewers).
3. **Tool/source guidance**: exact files/paths/commands to consult, and what NOT to consult.
4. **Boundaries**: file scope (write allowlist), off-limits paths, read-only vs write, word caps.

## Extensions

5. **Tool-call estimate + effort** (rules/13 § Per-Worker Defaults, § Effort Assignment, § Turn Caps): state a tool-call estimate per class for the worker's own pacing: recon 3-10; standard build/extract/review 10-20; complex multi-file 20-40. The estimate never selects the model: the model is fixed per worker (rules/13 table), and meta switches it only through the table's "Model switch" column. Meta tunes depth by effort (default +-1 level within the worker's range) and states the chosen effort and the one-line reason in the dispatch. Never dispatch `model: fable`. `maxTurns` (rules/13 § Turn Caps) is the backstop: hitting it means checkpoint + STATUS: PARTIAL, and the run is resumable via SendMessage. Do not split a task only to stay under a call count.
6. **Checkpoint-first**: the dispatch names a checkpoint path (default `~/.claude/plans/<campaign>/checkpoints/<task-id>.md`). The worker creates it within its first 5 tool calls (plan + scope), then appends findings/progress after each numbered check, file or corpus pass, never only at the end (a turn-cap hit or crash otherwise leaves nothing on disk; MC-4, 3 occurrences); the final message may be a pointer + summary. The parent never depends on the final message for load-bearing content. If the worker approaches its output budget before the task is finished, it must STOP and report exactly what is complete, what remains, and any uncommitted or partial state; it must never fabricate completion, silently drop work, or weaken/skip the task to fit. Producers close with `STATUS: PARTIAL`; reviewers close with a partial verdict noting the incomplete scope instead. Either way, the checkpoint plus a clean partial report let the parent finish or re-dispatch (see `/recover-truncated`).
7. **Reviewer isolation**: dispatches to reviewers include artifact + diff + rubric ONLY. Never the producer's reasoning, self-assessment, or prior clean verdicts.

## One job, done means stop

Applies to producers (w-implementer, w-refactorer, w-debugger, w-doc). Evidence: plans/worker-hygiene/checkpoints/MINE-1.md.

- After the final edit a producer runs ONE smoke check (the changed code on a realistic sample, or one targeted test; at most 2 runs after the last edit), writes the checkpoint, and hands back.
- No full suite, no reruns, no lint/grep sweeps, no self-review pass after the last edit: those belong to w-tester and w-reviewer, which meta dispatches in background (W-4).
- Interim checks DURING the work are fine: trying logic on a sample before applying it to everything, a targeted test right after a change, reproducing a bug.
- The hand-back carries a `NEXT-CHECKS:` block: the exact test commands (with paths/selectors) for w-tester and the review focus for w-reviewer.
- Before hand-back the worker stops every background task it started (TaskStop, or `scpy job.py stop <id>`). A report is final only with nothing left running.

## No waiting

Applies to every worker. A PreToolUse wait guard enforces most of these shapes (blocks or rewrites the call).

- Never run: `tail -f` / `tail -F`, `Get-Content -Wait`, `watch`, `until` / `while` loops that sleep, `for` loops that poll a file and break, `sleep` / `Start-Sleep` of 30 s or more, `find` over `/`, a drive root or the home root, `grep -r` over `~/.claude` (it contains `.venv` and `tmp`; use the Grep tool with a scoped path), a bare `python -` with no heredoc or pipe (it waits on stdin forever).
- Wrap any command that might be slow as `timeout <N> <cmd>`. The tool's own timeout does NOT kill a command: it moves it to the background, where it holds the worker resident.
- A job expected to run over 5 minutes (downloads, rate-limited probes, pipelines, long builds) is not the worker's to babysit: write and smoke-test the script on a sample, then hand back with the launch command; meta launches it.
- Never wait on another agent's file or checkpoint: hand back `STATUS: BLOCKED` naming the dependency.
- For a bounded in-task wait under about 9 minutes use the job helper, never a hand-rolled loop:
  - `scpy job.py start --name <id> [--marker <regex>] [--cwd <dir>] -- <cmd...>`: detached, returns at once; output in `~/.claude/tmp/jobs/<id>/out.log`, state in `status.json`.
  - `scpy job.py status <id>`: one-shot, never blocks.
  - `scpy job.py wait <id> [--max 540] [--stall 300] [--marker <regex> | --no-marker]`: returns on exit, marker, stall or `--max` (clamped to 580); `--no-marker` waits for the real exit even when `start` set a marker; exit 0 = ok or marker, 1 = non-zero exit, stopped or crashed, 2 = still running, 3 = stalled, 4 = usage error or unknown id. Commands are exec form; for a pipeline use `-- bash -c "..."`.
  - `scpy job.py stop <id>`; `scpy job.py list`.

## Constants

- Swarm ledger keys on **agentId**, never name (respawned names are refused sends). Explore/Plan are one-shot: anything steerable uses a custom w- type.
- On worker failure: steer/resume before respawn; the failure end-message carries last output (R-3 extension: resume-never-redo).
- 5 workers per batch, hard cap. Uncertain calls never batched with safe ones.
- Trigger escaping per rules/13 § Trigger Escaping: the three owner tokens stay dot-escaped in any authored prompt.
- R-1: if 2+ workers share an output artifact, pre-commit the schema in the dispatch or sequence the workers.
- Skill-granted workers: preloaded skills arrive with the worker; situational skills exist for the worker only if the brief names them (workers almost never load a skill unprompted). Meta names the matching ones from the worker's situational list (rules/13 § Worker Skills; per-worker list in the agent body); a worker may load an unnamed skill from its own situational list only when the task plainly matches it. Every skill outside the worker's preload and situational lists is off-limits (menus include CLI built-ins, plugin skills, and owner-opt-in commands that workers must never fire).
