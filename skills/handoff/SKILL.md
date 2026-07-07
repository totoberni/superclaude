---
name: handoff
description: "Use when the user explicitly asks to commission, continue, or decommission a permanent ork, or to hand off the current session."
category: orchestration
user-invocable: true
argument-hint: "[--commission <project> [name]] [--continue] [--decommission [--dry-run] [name]] [<target>]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# /handoff — Permanent-Ork Lifecycle Manager

## Unattended-context gate

This skill performs a mutating or irreversible operation and is now model-invocable. If it is invoked WITHOUT an explicit human instruction to perform this exact action in the CURRENT session, print the proposed mutation (the exact command or change it would make) and STOP; do not execute. Proceed only when a human has explicitly requested this action this session.

Focused on **permanent-ork lifecycle** (commission + decommission). Status checks are thinned to a delegation hint (use `/super-health --quick` and `/nudge`). Parallel batch dispatch lives in `/swarm-dispatch`.

Parse `$ARGUMENTS`: `--commission` | `--continue` | `--decommission` | `<target>` (default: session handoff)

---

## Swarm-First Default — When NOT to Use This Skill

This skill scaffolds **permanent orks** for multi-session, persistent-state work. **It is the EXCEPTION path**, not the default.

**Default delegation pattern is Meta+w-swarm** — see `~/.claude/rules/13-worker-first-mandate.md` § Decision Boundary. Use a swarm (not an ork) when ALL of:

- Estimated wall-clock ≤4 hr
- <8 distinct subtasks with clean independent scopes
- No persistent compile-gate ↔ edit-loop coupling
- <1M total context for synthesis
- Single-project scope

For ephemeral one-off workers: use `/autocommission` (separate skill, auto-cleanup on done).

For canonical parallel batch patterns (W-1 discovery / W-4 reviewer-BG / W-7 mixed / W-11 polish): use `/swarm-dispatch`.

**Use `/handoff` (this skill) when ANY of**:
- Multi-day campaign (HPC training, ACM-style multi-hour assembly)
- Persistent compile-gate ↔ edit-loop coupling required
- Multi-orch parallelism in same repo (already needs ork-tier coordination)
- Multi-session continuity required (ork preserves identity across hard-blocks)
- HW EDA pipelines (DC synth → gate-sim → example-tool loop)

If your work matches the swarm criteria above, do NOT scaffold an ork. Spawn a swarm directly.

---

## Authority & Caps

| Action | Who can invoke | Cap | Auto-cleanup |
|--------|---------------|-----|--------------|
| `--commission` (new ork) | meta only | none (limited by available terminals) | no — orks are persistent |
| `--continue` (status check) | meta + orch (self-check) | none | n/a |
| `--decommission` (archive) | meta only | none | yes — moves comms/<orch>/ → comms/_archive/ |
| Default session-handoff | meta only | once per session | no |

For ephemeral one-off workers: use `/autocommission` (auto-cleanup, meta+orch authority, unlimited cap per DEC-005).
For canonical parallel batch patterns: use `/swarm-dispatch`.

---

## HCOM Dual-Write (Phase B)

Each comms artifact written to flat-file is ALSO sent to the HCOM SQLite broker (`~/.claude/comms/.broker.db`). Pattern: **flat-write first (canonical), SQLite-write second (fail-soft)**. Flat remains source of truth in Phase B; SQLite mirror enables HCOM mid-turn injection and queryability.

Paste this helper at the top of skill execution and call after each comms-file mutation:

```bash
hcom_send() {
  # args: from_agent  to_agent  kind  seq(optional)  body
  local from="$1" to="$2" kind="$3" seq="$4" body="$5"
  "$HOME/.claude/.venv/bin/python" "$HOME/.claude/scripts/hcom-broker.py" send \
    --from "$from" --to "$to" --kind "$kind" \
    ${seq:+--seq "$seq"} \
    --body "$body" 2>/dev/null \
    || echo "Warning: HCOM send failed (broker unavailable)" >&2
}
```

Rules:
- Flat-file write must succeed first; SQLite write is a *mirror*, never a *prerequisite*.
- If the broker DB is missing or fails: log to stderr and continue. Do NOT roll back the flat write.
- Use `--from "meta"` for meta-originated artifacts (commission/decommission/handoff).
- `seq` is the directive number (1 for DIR-001, 2 for DIR-002, etc.); omit for non-numbered events.
- Kind taxonomy: `DIR` (directives), `RPT` (reports), `ESC` (escalations), `NUDGE` (status pings), `EVENT` (lifecycle: commission, decommission, archive).

**Backfill (future work)**: historical flat-file comms can be replayed into SQLite via a future `~/.claude/scripts/hcom-backfill.sh` (NOT implemented yet — would parse existing `comms/<orch>/{directives,reports,escalations}.md` and ingest into SQLite). Out of scope here.

---

## Mode 1: Commission (`--commission <project> [name]`)

**Note**: Initial state stub creation removed — per `~/.claude/rules/12-agent-hierarchy.md` § Write Scope, Meta writes master `plans/<name>/state.md`. Per-orch `state-<X>.md` is created by the orch on first session.

1. **Survey**: Read `plan.md`, `meta-registry.md`; fetch project pitfalls from DB:
   `HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py search '<project> gotchas mistakes' -k 8`
2. **Decide (DEC-NNN)**: Reuse idle orch if scope fits, else scaffold new. Record rationale.
3. **Scaffold** — create **all 5 artifacts** (missing any = broken orch):

| # | Artifact | Location |
|---|----------|----------|
| 1 | Agent alias | `~/.claude/agents/<name>.md` (template: `docs/usage-guide.md`, name: `o-<project>-<seq>`). Main agents (`meta`, `orch`, `scaf`, `o-*`) use `opus[1m]` with `--effort max`; subagents (`w-*`) use `opus` with `--effort max`. |
| 2 | Comms dir | `~/.claude/comms/<name>/` with `{directives,bootstrap,reports,escalations}.md` |
| 3 | DIR-001 | `comms/<name>/directives.md` — include `### Known Pitfalls` (3-7 items) |
| 4 | Bootstrap | `comms/<name>/bootstrap.md` — identity, env, top 3 pitfalls inline, plan/state refs |
| 5 | Registry | Append to `~/.claude/comms/meta-registry.md` Active table |

4. **HCOM mirror (dual-write)** — after the flat-file DIR-001 lands in `comms/<name>/directives.md`:

```bash
hcom_send "meta" "@<name>" "DIR" "1" "$(cat ~/.claude/comms/<name>/directives.md)"
hcom_send "meta" "*"       "EVENT" ""  "Commissioned orch <name> for project <project>"
```

The DIR mirror lets the new orch's `hcom-pre-tool-use.sh` hook inject the directive on its first tool call. The EVENT broadcast (`to=*`) lets other orchs/meta observe lifecycle without polling registry.

Tell the user: "Start `claude --agent <name>` in a new terminal."

---

## Mode 2 — `--continue` (status check, Phase D-full SQLite-only)

Lightweight check. For deeper signals, use:
- `/super-health --quick` for infra health
- `/nudge <orch>` for in-flight orch status
- `/comms-query stuck-orks` / `unanswered-esc` / `orphan-dir` for richer queries

### Procedure

```bash
# Phase D-full: broker is canonical. No flat-file fallback.
DB="$HOME/.claude/comms/.broker.db"
[ -f "$DB" ] || { echo "ERROR: HCOM broker unavailable ($DB). Run hcom-init.sh + hcom-backfill.sh."; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "ERROR: sqlite3 CLI required for Phase D"; exit 1; }

sqlite3 -header -column "$DB" "
  SELECT
    CASE WHEN to_agent LIKE '@%' THEN substr(to_agent, 2) ELSE from_agent END AS orch,
    datetime(MAX(ts), 'unixepoch') AS last_activity,
    MAX(CASE WHEN kind='RPT' THEN seq END) AS last_rpt,
    SUM(CASE WHEN kind='ESC' AND read_at IS NULL THEN 1 ELSE 0 END) AS unanswered_esc
  FROM messages
  WHERE ts > strftime('%s', 'now') - 604800
  GROUP BY orch
  ORDER BY MAX(ts) DESC;
"
```

Output is a table of: orch | last_activity | last_rpt | unanswered_esc.

### Phase D-full discipline

This skill is SQLite-only. No flat-file fallback. If the broker DB is missing or sqlite3 unavailable, the skill fails with a clear error message — initialize HCOM (`hcom-init.sh`) and backfill (`hcom-backfill.sh --apply --archive`) to recover.

The flat-file `comms/<orch>/reports.md` is a snapshot for human inspection (Phase B dual-write keeps it current) but is no longer the agent read path.

---

## Mode 3 — DELETED (use `/swarm-dispatch` instead)

**SOT for parallel work**: `/swarm-dispatch` (W-1/W-4/W-7/W-11 patterns) handles batch dispatch including conflict-graph + worktree setup hints for same-repo parallel orks.

---

## Mode 4: Decommission (`--decommission [name]`)

Archive a completed/idle orch. If `name` omitted, use the most recently delegated orch from the current conversation (infer from context — last orch mentioned in reports/directives discussion).

1. **Resolve target**: if no name given, scan conversation for the last orch discussed. Confirm with the user before proceeding.
2. **Pre-flight checks**:
   - Query broker for last RPT status:
     ```bash
     DB="$HOME/.claude/comms/.broker.db"
     sqlite3 -column "$DB" "SELECT seq, substr(body,1,120) FROM messages WHERE kind='RPT' AND from_agent='<name>' ORDER BY ts DESC LIMIT 1;"
     ```
     Verify status is DONE or COMPLETE.
   - Query broker for unanswered ESCs:
     ```bash
     sqlite3 -column "$DB" "SELECT COUNT(*) FROM messages WHERE kind='ESC' AND from_agent='<name>' AND read_at IS NULL;"
     ```
     Verify count is 0.
   - Check for unmerged branches: `git -C <repo> branch --no-merged main | grep <related-branch>` — warn if found
   - If any check fails: print warnings, ask the user to confirm before proceeding
3. **Cleanup** — remove stale working files before archiving:

| # | Action | Command |
|---|--------|---------|
| 1 | Clear directives | Overwrite `comms/<name>/directives.md` with decommission stub (preserve header, note date) |
| 2 | Clear bootstrap | Overwrite `comms/<name>/bootstrap.md` with decommission stub |
| 3 | Delete per-orch state | `rm comms/<name>/state.md` (if exists) |
| 4 | Delete legacy state | `rm plans/<project>/state-<name>.md` (if exists — historical convention) |
| 5 | Archive instance memory | `mv agent-memory/instance/<name> agent-memory/instance/archive/<name>-<date>` (if exists) |

4. **Archive** — execute in order:

| # | Action | Command |
|---|--------|---------|
| 1 | Move comms | `mv ~/.claude/comms/<name> ~/.claude/comms/_archive/<name>` |
| 2 | Append to archive log | Edit `comms/_archive/registry-history.md` — add `- <name> (<scope summary> -- DONE, decommissioned <date>)` under Archived Orchs |
| 3 | Remove from registry | Edit `meta-registry.md` — delete row from Idle/Active table |
| 4 | Delete agent alias | `rm ~/.claude/agents/<name>.md` (if exists) |

5. **HCOM mirror (dual-write)** — after the flat archive completes:

```bash
hcom_send "meta" "*" "EVENT" "" "Decommissioned orch <name>: archived to _archive/"
```

Broadcast (`to=*`) so any other orch/meta with HCOM mid-turn injection sees the decommission immediately. SQLite history is preserved (we never delete rows in Phase B).

6. **Report**: Print summary — what was archived/cleaned, unmerged branch warnings (if any), remaining active orchs.

### --dry-run (safety preview)

Optional flag: `/handoff --decommission --dry-run <orch-name>`

Prints what WOULD be archived (comms dir → _archive, agent file → agents/_archive, registry row update) without actually performing any moves. Always preview destructive ops on first decommission of unfamiliar orks.

---

## Default: Session Handoff (`<target>` or no args)

1. Summarize: objective, completed, remaining, files modified (`git diff --name-only`), decisions, blockers
2. Write to: `comms/<target>/bootstrap.md` (orch target) | `.orchestrator/handoff-<date>.md` (in-project) | chat (no target)
3. Include memory load order for receiver: `instance/<agent>` tier → `shared-projects` tier → `class` tier → `shared-global` tier (all queried via `memory_db.py search` or `list --tier <t>`)
4. **HCOM mirror (dual-write)** — only when target is an orch (skip for chat-only and in-project file handoffs):

```bash
hcom_send "meta" "@<target>" "EVENT" "" "Session handoff to @<target>: $(git -C <repo> diff --name-only | head -5 | paste -sd, -)"
```

Lets the receiving orch see the handoff event the moment it processes its next tool call, even before it reads `bootstrap.md`.