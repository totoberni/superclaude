---
name: super-health
description: "Use when scoring superclaude health across its subsystems"
category: memory
user-invocable: true
argument-hint: "[--json]"
allowed-tools: Read, Bash, Glob, Grep
---

# Superclaude Health Assessment (/100)

Read-only aggregate score over the six subsystems of the native Windows install at `~/.claude`.
Never modifies anything: the only writes happen inside OS temp directories (hook smoke sandbox,
pytest junit files), no `__pycache__` is left in the repo, git runs with `--no-optional-locks`.

**Mode**: $ARGUMENTS (default: markdown report. `--json` prints the same data as JSON.)

## Implementation (canonical runner)

`scpy health/super_health.py $ARGUMENTS` is the authoritative deterministic implementation.
It prints the component table (component, weight, score/100, weighted points, one-line detail),
the findings list, the grade, and as its final line exactly `SCORE: <int>/100`. Run it and
present its output. The tables below document what the script implements; the script is the
source of truth (`COMPONENTS`, `PENALTIES`, `RATIO_RULES`, `GRADES`, `DROPPED_TERMS`), and
`--json` also emits the penalty tables as data.

- Runtime: about 35 s, dominated by the pytest suites. Warn the user before running it in a
  tight session.
- Exit code 0 whenever the runner ran (the score is data, not an exit code); exit 2 only on an
  internal crash, with the traceback on stderr.
- `--root <dir>` points the runner at another install tree (used by the unit tests).

## Components and weights

| Component | Weight | What it checks |
|---|---:|---|
| Hooks | 20 | `hooks/**/*.py` compile (in memory); `hooks/hooks.json` parses; every hooks.json event is registered in `settings.json`; every settings hook names an existing interpreter, this install's `hooks/dispatch.py` and its own event; a sandboxed smoke run of `dispatch.py` per registered event; shell-form settings entries while hooks.json is exec-form |
| Skills | 15 | every `skills/*/SKILL.md` (not `_shared`): parseable YAML frontmatter, `name` equal to the directory, `description` starting "Use when", no `disable-model-invocation: true`, a `` `name` `` entry in `skills/README.md`; exactly one `## Unattended-context gate` in `skills/git/SKILL.md`; a `Consumed by:` line in each `skills/_shared/*.md`; every `.py` under `skills/` compiles |
| Memory | 15 | `scripts/memory/mem_health.py` (the `/mem-health` runner) in the venv; its final `SCORE` is the component score |
| Settings + agents | 15 | `settings.json` is a valid JSON object; every `agents/*.md` has frontmatter with `name` equal to the file stem, `model` in {haiku, sonnet, opus, inherit} (the first three also with the `[1m]` suffix), `effort` (if present) in {low, medium, high, xhigh, max}, and `tools`; every `w-*` agent cited in backticks in `rules/13-worker-first-mandate.md` or `CLAUDE.md` exists as `agents/<name>.md` and every `agents/w-*.md` is cited in rules/13; no leftover `.md` in `agents/_ephemeral/`; `bin/mem`, `bin/scpy` and the venv interpreter exist |
| Regression tests | 20 | pytest (venv, `-q -p no:cacheprovider`, `PYTHONDONTWRITEBYTECODE=1`) on `hooks/tests`, `scripts/tests`, `skills/notebook/tests` and any other `tests/` directory tracked in git outside `.venv` |
| Drift and hygiene | 15 | (a) lines in `rules/`, `agents/`, `skills/`, `CLAUDE.md` naming a dropped subsystem (curated `DROPPED_TERMS`, drawn from `docs/win11-migration/CONVENTIONS.md` §1, word-bounded, case-insensitive); (b) `/<name>` cross-references to a skill of the CONVENTIONS "Deleted skills" list, read at run time; (c) the `~/.claude` git working tree, informational only |

**Final score** = `round_half_up(sum(score * weight) / 100)`. Files are enumerated with
`git ls-files --cached --others --exclude-standard` (tracked or not ignored), so ignored trees
such as `skills/synced/` are not swept.

## Penalties

Each table component starts at 100 and loses the per-hit points of every finding. Each check has
a cap, so one noisy category can never zero a component on its own. A finding past the cap is
still listed, with `0 (capped)`.

**Hooks (20)**

| Check | Per hit | Cap | Deducted when |
|---|---:|---:|---|
| `py_compile` | 20 | 60 | a `hooks/**/*.py` file does not compile |
| `hooks_json` | 40 | 40 | `hooks/hooks.json` missing or not a JSON object |
| `event_unregistered` | 10 | 40 | a hooks.json event has no hook in `settings.json` |
| `hook_target` | 10 | 40 | a settings hook names a missing interpreter, a missing or foreign `dispatch.py`, or the wrong event |
| `smoke` | 10 | 40 | smoke run: exit != 0, timeout, non-JSON stdout, or no memory injection on SessionStart/SubagentStart |
| `handler_error` | 5 | 20 | a handler raised during the smoke run (fail-open, logged to the sandbox `hooks.log`) |
| `shell_form` | 5 | 5 | settings hooks are shell-form while hooks.json is exec-form: `bin/apply-settings.ps1 -Profile superclaude` not applied yet |

**Skills (15)**

| Check | Per hit | Cap | Deducted when |
|---|---:|---:|---|
| `frontmatter` | 10 | 40 | no parseable YAML frontmatter |
| `name` | 5 | 20 | `name` differs from the directory |
| `description` | 5 | 20 | `description` missing or not starting "Use when" |
| `model_invocation` | 5 | 20 | `disable-model-invocation: true` |
| `readme` | 3 | 15 | skill directory not listed in `skills/README.md` |
| `git_gate` | 10 | 10 | `skills/git/SKILL.md` missing or without exactly one gate heading |
| `shared_consumers` | 3 | 15 | a `skills/_shared/*.md` block without a `Consumed by:` line |
| `py_compile` | 10 | 30 | a `.py` under `skills/` does not compile |

**Settings + agents (15)**

| Check | Per hit | Cap | Deducted when |
|---|---:|---:|---|
| `settings_json` | 50 | 50 | `settings.json` missing or not a JSON object |
| `agent_frontmatter` | 10 | 40 | an agent has no parseable YAML frontmatter |
| `agent_name` | 5 | 20 | `name` differs from the file stem |
| `agent_model` | 5 | 20 | `model` outside the allowed set (for example `fable` or a full model id) |
| `agent_effort` | 5 | 20 | `effort` present but outside the allowed set |
| `agent_tools` | 5 | 20 | no `tools` field |
| `roster_missing` | 5 | 20 | a `w-*` cited in rules/13 or CLAUDE.md has no agent file |
| `roster_unlisted` | 5 | 20 | an `agents/w-*.md` not cited in rules/13 |
| `ephemeral_leftover` | 2 | 10 | a leftover `.md` in `agents/_ephemeral/` |
| `bin_shim` | 10 | 20 | `bin/mem` or `bin/scpy` missing |
| `venv` | 20 | 20 | `.venv/Scripts/python.exe` missing |

**Drift and hygiene (15)**

| Check | Per hit | Cap | Deducted when |
|---|---:|---:|---|
| `dropped_term` | 2 | 50 | a line names a dropped subsystem (one finding per line, all terms listed) |
| `deleted_slash` | 2 | 50 | a line cross-references a deleted skill as a slash command |

Slash commands of deleted skills are counted only by `deleted_slash`, never twice. If CONVENTIONS
has no "Deleted skills" line, the slash sweep is skipped and the detail column says so.

**Ratio components**

- Memory (15): score = the final `SCORE: N/100` of `mem_health.py`, already renormalised and
  capped by it (a criterion reported N/A caps it at 97). One finding carries the deduction and
  lists the criteria below full marks. Missing script or unparseable output scores 0.
- Regression tests (20): score = `floor(passed * 100 / (passed + failed + errors))`, skipped
  tests excluded; rounding down makes any red test cost at least one point. Each failing test is
  a finding at `file:line`. A suite that cannot run (missing directory, pytest exit other than
  0 or 1, which includes collection errors, or timeout) counts all its statically counted tests
  as failed.

## Hook smoke run

For each registered event the runner takes the interpreter and arguments from `settings.json`
(exec form `args` or shell form, both parsed) and runs them with a payload built by `json.dumps`
(benign: `echo` for PreToolUse, an `Agent` result for PostToolUse). `USERPROFILE` and `HOME` point
at a fresh OS temp directory, so every handler that writes state, logs, snapshots or backups
writes there; variables that redirect writes or switch guards off are removed. Pass means exit 0
within 10 s, stdout empty or a JSON object, and on SessionStart/SubagentStart a non-empty
`additionalContext`. Any entry in the sandbox `hooks.log` becomes a `handler_error` finding
naming the handler file: a fail-open hook that "passes" is still caught.

## Grades

| Score | Grade |
|---|---|
| >= 95 | A |
| >= 85 | B |
| >= 70 | C |
| >= 50 | D |
| < 50 | F |

## Reading the findings

Each finding reads `- [component/check] <points> `path[:line]`: detail`:

- `-N` is what that finding actually cost the component; `0 (capped)` means the check's cap was
  already reached; `ratio` marks per-test rows of the regression component (its deduction is the
  pass ratio, shown in the table).
- Paths are relative to `~/.claude`; `file:line` points at the offending line where one exists.
  For `readme` findings the path is `skills/README.md` (the row to add), for `shell_form` it is
  `settings.json` (the fix is running the named installer).
- The "Working tree" section lists `git status` entries for context only; it never changes the
  score.

When presenting: lead with the `SCORE` line and grade, then the component table, then the
findings grouped by component (largest deductions first) with one recommended action per check
class. Never recompute scores by hand and never soften a finding: if a check looks wrong, report
it as a suspected check bug next to the finding.
