---
name: better-super
description: "Mine and update superclaude tooling from upstream (two-wave, human-gated)."
category: maintenance
user-invocable: true
argument-hint: "[--new | --update]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

# /better-super — Upstream Mining and Dependency Currency

Two flags, each a two-wave flow with a mandatory human gate between waves. The gate is the contract: never auto-integrate or auto-apply.

## Purpose

Keep superclaude's tooling current by MINING upstream resources, not renting them. Mining means integrating the FEATURE into proprietary superclaude tooling adapted to our conventions (comms/hierarchy/memory). The upstream original becomes a stopgap to eventually drop.

`--new` discovers fresh upstream resources not yet mined, then (after your pick) integrates them.
`--update` catches up already-mined repos and pinned deps to their current versions, then (after your pick) applies the upgrades.

## When to Use

- `--new`: you want to scout what the broader Claude Code community has built that superclaude hasn't absorbed yet.
- `--update`: stopgap-registry repos have shipped new patterns; pinned python/npm/docker deps are drifting from upstream.

---

## `--new` — Discover, Gate, Integrate

### Wave 1: DISCOVER

**Step 1 — Crawler liveness check (mandatory)**

```bash
~/.claude/.venv/bin/python ~/.claude/scripts/better_super_crawl.py --health
```

If the check fails (container down), stop and tell the user:

```
Crawl4AI container is not running. Start it with:
  docker compose -f ~/.claude/services/crawl4ai/docker-compose.yml up -d
Then re-run /better-super --new.
```

**Step 2 — Load known state**

Read these files (targeted reads, not full dumps) to know what is already mined:

- `~/.claude/plans/superclaude-v3/mining-candidates.md` — already-evaluated candidates
- `~/.claude/upstream/curated-sources.md` — adopted sources + evaluation decisions
- `~/.claude/upstream/web-resources.md` — 325-link seed catalog (names + URLs only; do not full-read)
- `~/.claude/upstream/awesome-claude-code/THE_RESOURCES_TABLE.csv` — 215-entry catalogue

**Step 3 — Dispatch ≤5 parallel read-only research helpers**

Each helper uses `better_super_crawl.py` to fetch one upstream source as markdown, then scans for candidates NOT already in mining-candidates.md or curated-sources.md. Assign sources so helpers do not overlap:

Suggested source split (adjust based on what is stale):
1. awesome-claude-code README + any new category pages not in the CSV
2. `web-resources.md` links relevant to hooks/skills/agents (pick ≤10 unvisited high-signal URLs)
3. GitHub search: `topic:claude-code topic:hooks` or `topic:claude-code topic:skills` (fetch search result page via crawl)
4. GitHub search: `topic:claude-code topic:agents` or trending CC tool repos
5. Any curated-sources.md "WATCH" entries not yet re-evaluated

Helper instructions (embed in each spawn prompt):
- Fetch the assigned URL(s) via `~/.claude/.venv/bin/python ~/.claude/scripts/better_super_crawl.py --md <url>`
- Extract tool/pattern name, one-line purpose, and source URL for each candidate
- Skip anything already listed in mining-candidates.md or adopted in curated-sources.md
- Output ≤10 candidates as a bullet list: `name · what it does · source URL`
- Cap output at 400 words

**Step 4 — Rank and tabulate**

Synthesize helper outputs. Rank by ROI for owner's profile: ~50% academic ML/LaTeX, ~25% HPC/sim, ~25% SWE/web. Prefer candidates that are: a hook or skill (low integration effort), MIT/Apache-2.0 licensed, and not already covered by existing tooling.

Present the candidate table:

```
## /better-super --new: Discovered Candidates

| Name | What | Why ROI | Est. effort | Source URL |
|------|------|---------|-------------|------------|
| ...  | ...  | ...     | S/M/L       | ...        |

Sources scanned: [list]
Already-mined / skipped: N entries matched existing state.
```

---

### ** HUMAN GATE — stop here. Wait for owner to pick.**

Do not proceed. Present the table and ask: "Which of these would you like to integrate? (names or row numbers; 'none' to skip)"

---

### Wave 2: INTEGRATE

For each picked candidate:

1. **Fetch full source** via `better_super_crawl.py --md <source-url>` (or `--batch` for multiple).
2. **Adapt, do not copy-paste.** Strip the upstream agent's own hierarchy, memory, and comms conventions. Translate the useful PATTERN into superclaude's conventions:
   - Hooks go in `~/.claude/hooks/` (follow existing module structure)
   - Skills go in `~/.claude/skills/<name>/SKILL.md`
   - Rules go in `~/.claude/rules/`
   - Agents go in `~/.claude/agents/`
3. **If the integration needs a new dependency**, self-register it before writing any code that uses it:

   Python example:
   ```bash
   ~/.claude/scripts/better-super-deps.sh --record --type python --name <pkg> --version <ver> \
     --via better-super --reason "<one line why>"
   ~/.claude/scripts/better-super-deps.sh --pip-install
   ```

   Docker example:
   ```bash
   ~/.claude/scripts/better-super-deps.sh --record --type docker --name <name> --image <img> \
     --tag <tag> --compose <compose-path> --via better-super --reason "<why>"
   ```

4. **Append the new tool to `mining-candidates.md`** (provenance record — what was mined, from where, on what date, what was adapted into).
5. **If the upstream original supersedes something already in the stopgap registry**, update that entry in `~/.claude/docs/stopgap-registry.md`.

---

## `--update` — Check, Gate, Apply

### Wave 1: CHECK

**Step 1 — Crawler liveness check (mandatory)**

```bash
~/.claude/.venv/bin/python ~/.claude/scripts/better_super_crawl.py --health
```

Same failure message as `--new` if down.

**Step 2 — Dependency currency check**

```bash
# Python deps (sandbox-safe — PyPI live check):
~/.claude/scripts/better-super-deps.sh --check --type python

# npm/docker deps (network-restricted in sandbox):
~/.claude/scripts/better-super-deps.sh --check --type npm
~/.claude/scripts/better-super-deps.sh --check --type docker
```

For npm/docker rows, the tool returns `needs-network` status when sandboxed. To get live results, note that these require an unsandboxed shell (`dangerouslyDisableSandbox: true`) or the user can run them directly. Report `needs-network` rows clearly — do not pretend they were checked.

Capture the full `--check` output. Build a diff table showing `current → latest` for any out-of-date entries.

**Step 3 — Dispatch ≤5 parallel repo-scan helpers**

One helper per stopgap-registry entry (or group small ones). Each helper:
- Fetches the upstream repo's releases/changelog page via `~/.claude/.venv/bin/python ~/.claude/scripts/better_super_crawl.py --md <releases-url>`
- Reports: latest release version, release date, and any new patterns/features since the version last mined (check mining-candidates.md for the mined-at date)
- Caps output at 300 words

Stopgap repos to scan (from `~/.claude/docs/stopgap-registry.md`):
- wakamex/ccusage — `https://github.com/wakamex/ccusage/releases`
- ryoppippi/ccusage — `https://github.com/ryoppippi/ccusage/releases`
- overleaf-mcp — check project's releases or commits page
- pedrohcgs passport.yaml — check repo releases
- ralph-orchestrator — check repo releases/commits
- superpowers skills — `~/.claude/upstream/superpowers/` (local clone; read CHANGELOG or git log)

**Step 4 — Tabulate upgrade opportunities**

```
## /better-super --update: Upgrade Opportunities

### Dependency currency
| Name | Type | Current | Latest | Status |
|------|------|---------|--------|--------|
| ...  | py   | 1.2.3   | 1.4.0  | outdated |

### Stopgap repo changes
| Repo | Last mined | Latest release | New patterns? | Mined-into |
|------|-----------|----------------|---------------|------------|
| ...  | 2026-03-14 | v2.1.0         | yes — see note | /swarm-dispatch |
```

---

### ** HUMAN GATE — stop here. Wait for owner to pick.**

Do not proceed. Present both tables and ask: "Which upgrades would you like to take?"

---

### Wave 2: APPLY

For each picked item:

**Python dep upgrade:**
```bash
~/.claude/scripts/better-super-deps.sh --record --type python --name <pkg> --version <new-ver> \
  --via better-super --reason "bumped from <old> — <change note>"
~/.claude/scripts/better-super-deps.sh --pip-install
```

**Docker image upgrade:**
```bash
# Pull new image (run unsandboxed if container daemon is unreachable):
docker compose -f <compose-path> pull <service>
# Then update the manifest:
~/.claude/scripts/better-super-deps.sh --record --type docker --name <name> --image <img> \
  --tag <new-tag> --compose <compose-path> --via better-super --reason "bumped to <new-tag>"
```

**Stopgap-repo pattern re-mine:**
1. Fetch updated source via `better_super_crawl.py --md <repo-url>`.
2. Identify what changed since last mine. Extract only the delta patterns worth integrating.
3. Apply to the relevant superclaude file (skill, hook, rule) — minimal targeted edits.
4. Update the stopgap registry entry's `--update action` field to reflect what was applied and on what date.

**After all applies:** update `~/.claude/dependencies.yml`'s `updated:` field to today's date (the `--record` calls do this automatically per entry; verify the top-level `updated:` field is also current).

---

## Human Gate Contract

The gate is non-negotiable for both flags. It exists because:
- Discovery produces false-positives; owner must evaluate ROI before any tooling changes.
- Upgrades may break working integrations; owner decides which risks to accept.
- Auto-integration would violate the mining-not-renting principle — a dependency you didn't consciously adopt is a dependency you don't control.

**Never** auto-proceed from Wave 1 to Wave 2. If running in a non-interactive context (piped, backgrounded), report the Wave 1 table to stdout and exit with a clear message: "Human gate required — re-run interactively to proceed to Wave 2."

---

## Engines

| Engine | Location | Used for |
|--------|----------|---------|
| Crawl4AI client | `~/.claude/scripts/better_super_crawl.py` | Fetch upstream URLs as markdown |
| Deps manifest tool | `~/.claude/scripts/better-super-deps.sh` | Dependency check, record, install |
| Deps SSOT | `~/.claude/dependencies.yml` | Single source of truth for all pinned deps |
| Mining candidates | `~/.claude/plans/superclaude-v3/mining-candidates.md` | Provenance log of what has been evaluated |
| Stopgap registry | `~/.claude/docs/stopgap-registry.md` | Repos mined but still in use as originals |
| Upstream catalog | `~/.claude/upstream/awesome-claude-code/THE_RESOURCES_TABLE.csv` | 215-entry reference (--new Wave 1) |
| Curated sources | `~/.claude/upstream/curated-sources.md` | Adopted sources + evaluation decisions |
| Web resources seed | `~/.claude/upstream/web-resources.md` | 325-link seed catalog (--new Wave 1) |

**Crawl4AI container** (`docker compose -f ~/.claude/services/crawl4ai/docker-compose.yml up -d`): must be running for any wave that crawls external URLs. Always `--health`-check before crawling.

**Crawl4AI CLI reference:**
```
~/.claude/.venv/bin/python ~/.claude/scripts/better_super_crawl.py --health
~/.claude/.venv/bin/python ~/.claude/scripts/better_super_crawl.py --md <url>
~/.claude/.venv/bin/python ~/.claude/scripts/better_super_crawl.py --batch <url1> <url2> …
```

**Deps CLI reference:**
```
~/.claude/scripts/better-super-deps.sh --list [--type python|npm|docker]
~/.claude/scripts/better-super-deps.sh --check [--type python|npm|docker]
~/.claude/scripts/better-super-deps.sh --record --type T --name N --version V --via X --reason Y [--image/--tag/--compose/--endpoint/--scope/--install]
~/.claude/scripts/better-super-deps.sh --export [--out PATH]
~/.claude/scripts/better-super-deps.sh --pip-install [--dry-run]
```

---

## Principles

- **Mine, don't rent.** Integrate the pattern; retire the upstream original once internalized.
- **Adapt, don't copy-paste.** Every import must shed the upstream agent's hierarchy and adopt superclaude's comms/memory/write-scope conventions.
- **Gate is load-bearing.** Both flags STOP between waves. The user's pick is the architectural decision; the waves are execution.
- **Dep registration before use.** Self-register any new dependency before writing code that imports it. The manifest is SSOT.
- **Dot-escape trigger tokens.** The `.workflow`, `/.deep-research`, and `.ultracode` tokens are owner-opt-in only. They must never appear unescaped in this skill, in spawn prompts derived from it, or in any authored file another agent will process. Keep the leading dot.
- **Infra-scope.** This skill mutates superclaude's own tooling under `~/.claude/` — a meta/scaf-level operation; the executor must hold that write scope (an orch should not run the integrate/apply waves).
- **Delegate substantial writes.** Wave-2 integration and apply file-writes go through dispatched `w-implementer` / `w-doc` workers (swarm-first, scoped); reserve direct edits for small targeted deltas.
