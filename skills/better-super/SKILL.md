---
name: better-super
description: "Use when mining and updating superclaude tooling from upstream"
category: maintenance
user-invocable: true
argument-hint: "[--new | --update]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, WebFetch, WebSearch
---

# /better-super — Upstream Mining and Dependency Currency

Two flags, each a two-wave flow with a mandatory human gate between waves. The gate is the contract: never auto-integrate or auto-apply.

## Purpose

Keep superclaude's tooling current by MINING upstream resources, not renting them. Mining means integrating the FEATURE into proprietary superclaude tooling adapted to our conventions (hierarchy/memory/write scope). The upstream original becomes a stopgap to eventually drop.

`--new` discovers fresh upstream resources not yet mined, then (after your pick) integrates them.
`--update` catches up already-mined repos and pinned deps to their current versions, then (after your pick) applies the upgrades.

## When to Use

- `--new`: you want to scout what the broader Claude Code community has built that superclaude hasn't absorbed yet.
- `--update`: stopgap-registry repos have shipped new patterns; pinned python/npm deps are drifting from upstream.

## Fetching upstream pages

There is no local crawler service on this instance. Upstream pages are fetched with the built-in `WebFetch` tool (one URL, returned as markdown) and `WebSearch`; GitHub metadata (releases, tags, topics) is read with `gh` (`gh release list -R <owner>/<repo>`, `gh search repos --topic claude-code ...`). Local clones under `~/.claude/upstream/` are read with Read/Grep and `git -C <clone> log`.

---

## `--new` — Discover, Gate, Integrate

### Wave 1: DISCOVER

**Step 1 — Load known state**

Read these files (targeted reads, not full dumps) to know what is already mined:

- `~/.claude/plans/superclaude-v3/mining-candidates.md` — already-evaluated candidates
- `~/.claude/upstream/curated-sources.md` — adopted sources + evaluation decisions
- `~/.claude/upstream/web-resources.md` — 325-link seed catalog (names + URLs only; do not full-read)
- `~/.claude/upstream/awesome-claude-code/THE_RESOURCES_TABLE.csv` — 215-entry catalogue

**Step 2 — Dispatch ≤5 parallel read-only research helpers**

Each helper fetches one upstream source (WebFetch, or `gh` for GitHub), then scans for candidates NOT already in mining-candidates.md or curated-sources.md. Assign sources so helpers do not overlap:

Suggested source split (adjust based on what is stale):
1. awesome-claude-code README + any new category pages not in the CSV
2. `web-resources.md` links relevant to hooks/skills/agents (pick ≤10 unvisited high-signal URLs)
3. GitHub search: `topic:claude-code topic:hooks` or `topic:claude-code topic:skills` (`gh search repos`)
4. GitHub search: `topic:claude-code topic:agents` or trending CC tool repos
5. Any curated-sources.md "WATCH" entries not yet re-evaluated

Helper instructions (embed in each spawn prompt):
- Fetch the assigned URL(s) with WebFetch (or `gh` for GitHub repositories)
- Extract tool/pattern name, one-line purpose, and source URL for each candidate
- Skip anything already listed in mining-candidates.md or adopted in curated-sources.md
- Output ≤10 candidates as a bullet list: `name · what it does · source URL`
- Cap output at 400 words

**Step 3 — Rank and tabulate**

Synthesize helper outputs. Rank by ROI for owner's profile: ~50% academic ML/LaTeX, ~25% HPC/sim, ~25% SWE/web. Prefer candidates that are: a hook or skill (low integration effort), MIT/Apache-2.0 licensed, not already covered by existing tooling, and runnable on native Windows without admin rights (no container, no WSL, no POSIX-only shell scripts).

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

1. **Fetch full source** with WebFetch (or a shallow `git clone` under `~/.claude/upstream/`).
2. **Adapt, do not copy-paste.** Strip the upstream agent's own hierarchy, memory, and messaging conventions. Translate the useful PATTERN into superclaude's conventions:
   - Hooks are Python handlers under `~/.claude/hooks/sc_hooks/handlers/`, registered with `@on(event, tools, name)`; an upstream bash hook is ported, never dropped in as is
   - Skills go in `~/.claude/skills/<name>/SKILL.md`
   - Rules go in `~/.claude/rules/`
   - Agents go in `~/.claude/agents/`
3. **If the integration needs a new dependency**, self-register it before writing any code that uses it:

   ```
   scpy better_super_deps.py --record --type python --name <pkg> --version <ver> --via better-super --reason "<one line why>"
   uv pip install --python ~/.claude/.venv/Scripts/python.exe <pkg>==<ver>
   ```

   The venv is `~/.claude/.venv`, managed with `uv`. Install at user scope only; nothing on this machine may require admin rights.

4. **Append the new tool to `mining-candidates.md`** (provenance record — what was mined, from where, on what date, what was adapted into).
5. **If the upstream original supersedes something already in the stopgap registry**, update that entry in `~/.claude/docs/stopgap-registry.md`.

---

## `--update` — Check, Gate, Apply

### Wave 1: CHECK

**Step 1 — Dependency currency check**

```
scpy better_super_deps.py --check --type python
scpy better_super_deps.py --check --type npm
```

If a row comes back with a `needs-network` status, report it clearly — do not pretend it was checked.

Capture the full `--check` output. Build a diff table showing `current → latest` for any out-of-date entries.

**Step 2 — Dispatch ≤5 parallel repo-scan helpers**

One helper per stopgap-registry entry (or group small ones). Each helper:
- Reads the upstream repo's releases/changelog (`gh release list -R <owner>/<repo>`, or WebFetch on the releases page)
- Reports: latest release version, release date, and any new patterns/features since the version last mined (check mining-candidates.md for the mined-at date)
- Caps output at 300 words

Stopgap repos to scan (from `~/.claude/docs/stopgap-registry.md`):
- wakamex/ccusage — `https://github.com/wakamex/ccusage/releases`
- ryoppippi/ccusage — `https://github.com/ryoppippi/ccusage/releases`
- overleaf-mcp — check project's releases or commits page
- pedrohcgs passport.yaml — check repo releases
- ralph-orchestrator — check repo releases/commits
- superpowers skills — `~/.claude/upstream/superpowers/` (local clone; read CHANGELOG or git log)

**Step 3 — Tabulate upgrade opportunities**

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
```
scpy better_super_deps.py --record --type python --name <pkg> --version <new-ver> --via better-super --reason "bumped from <old> — <change note>"
uv pip install --python ~/.claude/.venv/Scripts/python.exe <pkg>==<new-ver>
```

**Stopgap-repo pattern re-mine:**
1. Fetch the updated source (WebFetch, `gh`, or `git -C <clone> pull --ff-only` for a local clone).
2. Identify what changed since last mine. Extract only the delta patterns worth integrating.
3. Apply to the relevant superclaude file (skill, hook handler, rule) — minimal targeted edits.
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
| Page fetch | built-in `WebFetch` / `WebSearch`, `gh` | Fetch upstream URLs and GitHub metadata |
| Deps manifest tool | `~/.claude/scripts/better_super_deps.py` (run as `scpy better_super_deps.py <flags>`) | Dependency check, record, export |
| Deps SSOT | `~/.claude/dependencies.yml` | Single source of truth for all pinned deps |
| Mining candidates | `~/.claude/plans/superclaude-v3/mining-candidates.md` | Provenance log of what has been evaluated |
| Stopgap registry | `~/.claude/docs/stopgap-registry.md` | Repos mined but still in use as originals |
| Upstream catalog | `~/.claude/upstream/awesome-claude-code/THE_RESOURCES_TABLE.csv` | 215-entry reference (--new Wave 1) |
| Curated sources | `~/.claude/upstream/curated-sources.md` | Adopted sources + evaluation decisions |
| Web resources seed | `~/.claude/upstream/web-resources.md` | 325-link seed catalog (--new Wave 1) |

**Deps CLI reference:**
```
scpy better_super_deps.py --list [--type python|npm]
scpy better_super_deps.py --check [--type python|npm]
scpy better_super_deps.py --record --type T --name N --version V --via X --reason Y [--endpoint/--scope/--install]
scpy better_super_deps.py --export [--out PATH]
```

---

## Principles

- **Mine, don't rent.** Integrate the pattern; retire the upstream original once internalized.
- **Adapt, don't copy-paste.** Every import must shed the upstream agent's hierarchy and adopt superclaude's memory/write-scope conventions.
- **Gate is load-bearing.** Both flags STOP between waves. The user's pick is the architectural decision; the waves are execution.
- **Dep registration before use.** Self-register any new dependency before writing code that imports it. The manifest is SSOT.
- **Dot-escape trigger tokens.** The `.workflow`, `/.deep-research`, and `.ultracode` tokens are owner-opt-in only. They must never appear unescaped in this skill, in spawn prompts derived from it, or in any authored file another agent will process. Keep the leading dot.
- **Infra-scope.** This skill mutates superclaude's own tooling under `~/.claude/` — a meta-level operation; the executor must hold that write scope.
- **Delegate substantial writes.** Wave-2 integration and apply file-writes go through dispatched `w-implementer` / `w-doc` workers (swarm-first, scoped); reserve direct edits for small targeted deltas.

---

## Post-apply infra-security review

Wave 2 above applies infra edits sourced from EXTERNAL upstream: an integrated hook handler, skill, rule, or agent, or a bumped dependency, lands live under `~/.claude/` on the human's mining pick. The human gate decides WHAT to apply; it does not verify that the APPLIED result is safe. A one-shot infra-security review closes that second gap.

**The human mining-gate stays separate and earlier.** The blocking human gate between Wave 1 and Wave 2 (pick what to mine or upgrade) is NEVER replaced by the review. The human decides WHAT lands; the review then verifies the applied edits are secure. A clean verdict never substitutes for the human pick, and the human pick never substitutes for the review.

After the Wave-2 producer returns `STATUS: DONE` (it never self-certifies the safety of its own edit), dispatch ONE fresh `w-reviewer` with the `/infra-security` rubric (sonnet for a single small rule or handler edit; opus for multi-file, settings-surface, or security-surface changes). It receives the applied edits + diff + rubric ONLY (reviewer isolation), examines the CURRENT edits with fresh evidence (no pre-approval), and runs the infra-security checklist over what landed: permission and write-ACL integrity, hook safety, implicit-execution and remote-code detection, agent-authority compliance, and the red-flag scan. It returns a `VERDICT:` line, which the parent quotes verbatim.

On `VERDICT: REWORK`, accept or contest each finding with evidence (file:line, the infra-security clause it violates, or a concrete exploit path), fix the accepted ones, and re-review only if the owner or meta judges it necessary. Any change to the applied edits after a `VERDICT: CLEAN` makes that verdict stale. **Authority**: the review dispatch needs spawn authority, so it runs from meta or the owner's session, never from a `w-*` worker.

## Cross-References

- Token protocol and severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Infra-security checklist (run by the reviewer): `~/.claude/skills/infra-security/SKILL.md`
- Dispatch contract: `~/.claude/skills/_shared/dispatch-contract.md`
