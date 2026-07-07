---
name: super-health
description: "Superclaude health /100 across 9 subsystems (always deepest); --complete adds 5-agent audit."
category: health
user-invocable: true
argument-hint: "[--complete]"
allowed-tools: Read, Bash, Glob, Grep, Agent
---

# Superclaude Health Assessment (/100)

Aggregate health score across all superclaude subsystems. Weighted combination of component scores.

**Depth**: invariant. Every run executes the deepest check set (no depth flag).

## Component Weights

| Component | Weight | Source Skill/Script |
|-----------|--------|---------------------|
| Hook health | 15% | `/hook-health` |
| Skill health | 12% | `/skill-health` |
| Memory health | 15% | `/mem-health` |
| Settings + agents | 12% | Inline checks (below) |
| Sessions | 5% | Inline checks (below) |
| Comms (HCOM broker) | 9% | Inline checks (below) — Phase D-full SQLite-only |
| Regression tests | 11% | `infra-test.sh` |
| Subsystems (v3) | 11% | Inline checks (below) |
| Automations engine | 10% | `automations-health.sh` (513-test WSL suite + toto runtime probe) |

**Final score** = sum of `component_score * weight` across all 9 components (= 100%).

## Depth (invariant) + `--complete`

There is NO depth flag. Every run always executes the deepest check configuration:
hook-health `--deep`, `infra-test.sh --full`, `automations-health.sh` (the toto probe
plus the 513-test WSL engine suite), and every structural / `bash -n` / `ss_*` sub-check.
Legacy `--quick` / `--standard` / `--deep` (and any unknown flag) are ignored gracefully
so old callers never break. Budget: ~20 min of model time if narrated; **warn about
session budget**.

`--complete` is ORTHOGONAL to depth (it does not change what the /100 measures). It
additionally runs the heavy script-hygiene scan and prints the Step-4 5-agent post-hoc
audit instruction (5 parallel general-purpose audit agents across {rules, agents, skills,
hooks, comms+memory}). **Warn about session budget AND helper usage.**

## Procedure

## Implementation (canonical runner)

`bash ~/.claude/scripts/super-health.sh $ARGUMENTS` is the authoritative deterministic
implementation of everything below. It calls `hook-health.sh`, `skill-health.sh`, and
`mem-health.sh` for those components, ports the inline settings/sessions/comms scoring,
runs `infra-test.sh` for the regression component, runs `automations-health.sh` for the
automations component, applies the weights table, and prints the component table + grade +
a final `SCORE: <int>/100` line. Run it and present its output. The criteria/weights tables
below document what the script implements; the `--complete` flag additionally requires the
model to run the Step-4 5-agent post-hoc audit (the script prints the instruction but never
spawns agents).

### Step 1: Run Component Health Checks

Execute each component and capture its /100 score.

#### 1a. Hook Health (18%)

Run `/hook-health` at the appropriate tier. Capture the `/100` score.

`score_hook` additionally scores a **subagent-stop ledger-hook facet** (wf-skills W1.6; `hook-health.sh` predates the hook). `hooks/subagent-stop.sh` must exist, be `bash -n` clean, and contain the STOP forensics row writer (a `printf` emitting a literal `\tSTOP\t` column). Bounded penalty (cap 6): adds an assertion, only lowers the hook score on regression, never relaxes a `hook-health` criterion.

#### 1b. Skill Health (13%)

Run `/skill-health all`. Capture the `/100` score.

`score_skill` additionally scores a **`_shared` rubric-block facet** (wf-skills W1.2). The 8 shared blocks (`verdict-schema`, `dispatch-contract`, `helper-prompt`, `retro-evidence`, `diff-target`, `discovery-protocol`, `search-budget`, `memory-distill`) are not skills (no SKILL.md) so `skill-health.sh` cannot see them: each must exist and carry a `Consumed by:` line. Bounded penalty (cap 8, `+1` per missing block, `+1` per block lacking `Consumed by:`); adds an assertion, only lowers on regression.

#### 1c. Memory Health (18%)

Run `/mem-health`. Capture the `/100` score.

The `score_mem` function in `super-health.sh` also runs a read-only `.memory.db` integrity facet (symmetric with the comms `.comms.db` facet in `score_comms`):

- File present and opens as valid SQLite → proceed; absent or `sqlite3` CLI absent → graceful skip (no penalty)
- Checks presence of tables `memories`, `memories_fts`, `memories_vec`, `memories_fts_docsize` via `sqlite_master`
- Row-count coherence: `COUNT(*) FROM memories` must equal `COUNT(*) FROM memories_fts_docsize` (the FTS5 doc-index shadow — using `memories_fts` directly would be vacuous because it is an external-content table that always proxies back to `memories`)
- `memories_vec` is a vec0 VIRTUAL table; plain `sqlite3` cannot `SELECT COUNT(*) FROM memories_vec`, so presence is asserted via `sqlite_master` only
- **Broken db → cap the whole mem component at 50** (real ceiling, not a cosmetic note)

**Corpus-health facet (two-pronged).** After the integrity facet, `score_mem` measures whether the corpus is an operationally healthy *size and shape* (distinct from the integrity cap, which answers whether the store is structurally sound). Both prongs are always surfaced; together they fold into ONE bounded `corpus_penalty` (cap **12**) — a single penalty avoids double-jeopardy with mem-health criterion 1.

- **(a) Searchability** — `row_count` (`COUNT(*)`, always) plus a measured search latency. The latency runs ONE real `memory_db.py search … -k 5`, timed (always, since depth is invariant); it is the memory facet's one python-spawning probe.
- **(b) Footprint** — `MIN`/`AVG`/`MAX(LENGTH(text))` over **non-exempt** rows (always; reuses the same line-anchored `<!-- budget-exempt` predicate as `/mem-health` criterion 1, so a legitimately-exempt giant does not distort the max).

Reference caps — *normal-operation reference points calibrated to the live corpus*, all env-overridable:

| Ref | Default | Env override | Penalty when exceeded |
|-----|---------|--------------|-----------------------|
| Non-exempt MAX bytes | 50000 | `MEM_MAX_BYTES_REF` | `+= min(8, round((max/REF − 1) * 20))` (graduated, capped at 8) |
| Non-exempt AVG bytes | 8000 | `MEM_AVG_BYTES_REF` | `+= 3` (flat) |
| Row count | 600 | `MEM_ROW_COUNT_REF` | `+= 3` (flat) |
| Search latency (ms) | 5000 | `MEM_SEARCH_LATENCY_REF_MS` | `+= 3` (flat) |

Final: `final_mem = max(0, min(mh_score, mdb_cap) − corpus_penalty)` — the integrity result is a hard ceiling (min), corpus bloat is a graduated deduction below it. Surfaced line: `Memory corpus facet: rows=N (ref 600), footprint min/avg/max=…/…/…B (max-ref 50000, exempt-aware), latency=<…>; penalty=−P`. `MEMORY_DB_PATH` overrides the DB path (the failing-path tests drive each prong over its ref against a throwaway copy or via a low env-ref).

#### 1d. Settings + Agents (12%)

Inline scoring — no separate skill needed:

| Criterion | Points | Check |
|-----------|--------|-------|
| settings.json valid JSON | 25 | `jq . ~/.claude/settings.json > /dev/null 2>&1` |
| Agent frontmatter valid | 25 | All agents have `---`, `name:`, `model:` |
| Model values valid | 15 | All `model:` values are `sonnet`, `opus`, `haiku`, or `[1m]` variant (e.g. `opus[1m]`) |
| Deny rules >= 5 | 15 | `jq '.permissions.deny \| length' ~/.claude/settings.json` |
| No orphan agents | 10 | Agent file without matching comms dir (excluding workers/base) |
| No broken symlinks | 10 | `find ~/.claude/agents/ -maxdepth 1 -type l ! -exec test -e {} \; -print` |

`score_settings` additionally applies a **wf-skills grants + fleet report-contract facet** (W1.1 / W1.7) on top of the 100-point award structure above (structure untouched; penalty applied on top, floored at 0). `meta.md` must grant SendMessage/Skill/WebSearch/WebFetch and `orch.md` SendMessage/Skill on their `tools:` lines; each `w-*.md` must carry exactly one `## Report Contract (wf-skills)` section. Bounded penalty (cap 10): adds an assertion, only lowers the score on regression, never relaxes or reweights an existing criterion.

```bash
CLAUDE="$HOME/.claude"
SCORE=0

# Valid JSON (25 pts)
jq . "$CLAUDE/settings.json" > /dev/null 2>&1 && SCORE=$((SCORE + 25))

# Agent frontmatter (25 pts)
TOTAL=0; VALID=0
for a in "$CLAUDE"/agents/*.md; do
  [ -f "$a" ] || continue
  TOTAL=$((TOTAL + 1))
  FM=$(sed -n '2,/^---$/p' "$a" | head -20)
  if head -1 "$a" | grep -q "^---$" && echo "$FM" | grep -q "^model:"; then
    VALID=$((VALID + 1))
  fi
done
[ "$TOTAL" -gt 0 ] && SCORE=$((SCORE + VALID * 25 / TOTAL))

# Model values valid (15 pts)
MODELS_OK=0; MODELS_TOTAL=0
for a in "$CLAUDE"/agents/*.md; do
  [ -f "$a" ] || continue
  MODEL=$(sed -n 's/^model: *//p' "$a" | head -1 | tr -d '"')
  [ -z "$MODEL" ] && continue
  MODELS_TOTAL=$((MODELS_TOTAL + 1))
  case "$MODEL" in opus|sonnet|haiku|"opus[1m]"|"sonnet[1m]"|"haiku[1m]") MODELS_OK=$((MODELS_OK + 1)) ;; esac
done
[ "$MODELS_TOTAL" -gt 0 ] && SCORE=$((SCORE + MODELS_OK * 15 / MODELS_TOTAL))

# Deny rules >= 5 (15 pts)
DENY=$(jq '.permissions.deny | length' "$CLAUDE/settings.json" 2>/dev/null || echo 0)
[ "$DENY" -ge 5 ] && SCORE=$((SCORE + 15))

# No orphan agents (10 pts)
ORPHANS=0
for a in "$CLAUDE"/agents/*.md; do
  [ -f "$a" ] || continue
  NAME=$(basename "$a" .md)
  case "$NAME" in w-*|orch|meta|w-design-reviewer) continue ;; esac
  [ -d "$CLAUDE/comms/$NAME" ] || ORPHANS=$((ORPHANS + 1))
done
[ "$ORPHANS" -eq 0 ] && SCORE=$((SCORE + 10))

# No broken symlinks (10 pts)
BROKEN=$(find "$CLAUDE/agents/" -maxdepth 1 -type l ! -exec test -e {} \; -print 2>/dev/null | wc -l)
[ "$BROKEN" -eq 0 ] && SCORE=$((SCORE + 10))

echo "Settings+Agents: $SCORE/100"
```

#### 1e. Sessions (5%)

Inline scoring:

| Criterion | Points | Check |
|-----------|--------|-------|
| No zombie timer files | 50 | PID unverifiable AND .start aged past plausible lifetime (>60 min; meta/`.override` >24 h) |
| No orphaned session files | 30 | .start without .agent or vice versa |
| Active sessions within limits | 20 | No non-exempt session >48 min (meta and `.override` are exempt) |

`scripts/super-health.sh` `score_session()` is the executable SOT; the block below documents the algorithm. `kill -0` is unreliable across PID namespaces (a sandboxed tool cannot see host PIDs), so liveness is corroborated with `.start` age, which is filesystem-derived and therefore namespace-independent — otherwise every live session is mis-flagged as a zombie when the check runs through a sandbox.

```bash
TIMER_DIR="$HOME/.claude/session-timers"
SCORE=0

if [ ! -d "$TIMER_DIR" ]; then
  echo "Sessions: 100/100 (no timer dir = clean)"
else
  NOW=$(date +%s)
  # No zombies (50 pts): a set is leaked only if its PID is unverifiable AND it
  # has outlived any plausible lifetime — 60 min for time-limited agents (53 min
  # hard block), 24 h for exempt meta / .override sessions.
  ZOMBIES=0
  for pf in "$TIMER_DIR"/*.pid; do
    [ -f "$pf" ] || continue
    PID=$(cat "$pf" 2>/dev/null)
    [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null && continue   # verifiably alive
    SID=$(basename "$pf" .pid)
    START=$(cat "$TIMER_DIR/${SID}.start" 2>/dev/null)
    if ! [[ "$START" =~ ^[0-9]+$ ]]; then ZOMBIES=$((ZOMBIES + 1)); continue; fi
    AGENT=$(cat "$TIMER_DIR/${SID}.agent" 2>/dev/null)
    if [ "$AGENT" = "meta" ] || [ -f "$TIMER_DIR/${SID}.override" ]; then BOUND=86400; else BOUND=3600; fi
    [ "$(( NOW - START ))" -gt "$BOUND" ] && ZOMBIES=$((ZOMBIES + 1))
  done
  [ "$ZOMBIES" -eq 0 ] && SCORE=$((SCORE + 50))

  # No orphans (30 pts)
  ORPHANS=0
  for sf in "$TIMER_DIR"/*.start; do
    [ -f "$sf" ] || continue
    SID=$(basename "$sf" .start)
    [ ! -f "$TIMER_DIR/${SID}.agent" ] && ORPHANS=$((ORPHANS + 1))
  done
  for af in "$TIMER_DIR"/*.agent; do
    [ -f "$af" ] || continue
    SID=$(basename "$af" .agent)
    [ ! -f "$TIMER_DIR/${SID}.start" ] && ORPHANS=$((ORPHANS + 1))
  done
  [ "$ORPHANS" -eq 0 ] && SCORE=$((SCORE + 30))

  # Within limits (20 pts) — meta and .override sessions are exempt from the limit.
  OVER=0
  for sf in "$TIMER_DIR"/*.start; do
    [ -f "$sf" ] || continue
    SID=$(basename "$sf" .start)
    [ "$(cat "$TIMER_DIR/${SID}.agent" 2>/dev/null)" = "meta" ] && continue
    [ -f "$TIMER_DIR/${SID}.override" ] && continue
    START=$(cat "$sf" 2>/dev/null)
    [[ "$START" =~ ^[0-9]+$ ]] || continue
    AGE_MIN=$(( (NOW - START) / 60 ))
    [ "$AGE_MIN" -gt 48 ] && OVER=$((OVER + 1))
  done
  [ "$OVER" -eq 0 ] && SCORE=$((SCORE + 20))

  echo "Sessions: $SCORE/100"
fi
```

#### 1g. Comms (HCOM broker, 10%)

Query the HCOM SQLite broker for comms-health signals. Phase D-full: broker is required — if unavailable, score is 0/100 (hard fail).

| Criterion | Points | Check |
|-----------|--------|-------|
| Unanswered ESC count = 0 | 30 | broker query: `SELECT COUNT(*) FROM messages WHERE kind='ESC' AND read_at IS NULL` (older than 30 min counts) |
| Stale-comms decluttered | 25 | Active comms dir that is >7d stale AND marked complete (decommissioned / final DONE) should be in `_archive/`. Deduct 5 per flagged dir. (Replaced the legacy outstanding-DIR check — live orks are tracked on-terminal, not via this metric.) |
| Schema-lint clean | 20 | Batch-lint of flat comms ledgers: every `## {DIR,RPT,ESC}-NNN` entry checked for its required fields (same rules as `comms-schema-lint.sh`); points scale well-formed/total. Full only when zero entries exist to lint. |
| Active orks count <= 5 | 15 | broker query: COUNT(DISTINCT orch) WHERE ts > now-7d |
| Backfill audit table populated | 10 | indicates HCOM has historical context |

```bash
CLAUDE="$HOME/.claude"
DB="$CLAUDE/comms/.broker.db"
SCORE=0

if [ ! -f "$DB" ]; then
  echo "Comms: 0/100 (HCOM broker unavailable — Phase D requires broker. Run hcom-init.sh.)"
  COMMS_SCORE=0
elif ! command -v sqlite3 >/dev/null 2>&1; then
  echo "Comms: 0/100 (sqlite3 CLI required for Phase D-full)"
  COMMS_SCORE=0
else
  # Unanswered ESC = 0 (30 pts; 30-min grace)
  UNANSWERED_ESC=$(sqlite3 "$DB" "SELECT COUNT(*) FROM messages WHERE kind='ESC' AND read_at IS NULL AND ts < strftime('%s','now') - 1800;" 2>/dev/null || echo 999)
  [ "$UNANSWERED_ESC" -eq 0 ] && SCORE=$((SCORE + 30))

  # Stale-comms declutter (25 pts) — implemented in super-health.sh score_comms (canonical):
  # flag active comms dirs that are stale (>7d since last file activity) AND marked complete
  # (decommissioned / final DONE) → they belong in comms/_archive/. Deduct 5 per flagged dir,
  # floor 0. Becomes DB-aware once comms moves to the FTS5+HTML DB (plan Phase 2 / T7.1b).
  # (No broker outstanding-DIR query — owner tracks live orks on his terminals; that was legacy noise.)

  # Schema-lint (20 pts) — REAL batch-lint of flat comms ledgers (see super-health.sh
  # score_comms): scans every "## {DIR,RPT,ESC}-NNN" entry for its kind's required
  # fields (same rules as comms-schema-lint.sh); points scale well-formed/total.
  # Full only when there are genuinely zero entries to lint. (No unconditional award.)

  # Active orks count ≤ 5 (15 pts)
  ACTIVE_ORKS=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT CASE WHEN to_agent LIKE '@%' THEN substr(to_agent, 2) ELSE from_agent END) FROM messages WHERE ts > strftime('%s','now') - 604800;" 2>/dev/null || echo 0)
  [ "$ACTIVE_ORKS" -le 5 ] && SCORE=$((SCORE + 15))

  # Backfill audit populated (10 pts) — confirms historical context retained
  BACKFILLED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM backfill_audit;" 2>/dev/null || echo 0)
  [ "$BACKFILLED" -gt 0 ] && SCORE=$((SCORE + 10))

  echo "Comms: $SCORE/100 (unanswered ESC=$UNANSWERED_ESC, active orks=$ACTIVE_ORKS, backfilled=$BACKFILLED; +stale-comms +schema-lint scored in super-health.sh)"
  COMMS_SCORE=$SCORE
fi
```

### Phase D-full discipline

Broker is required for Comms scoring. If unavailable: score = 0/100 (hard fail; not graceful — broker is canonical per Phase D). Recovery: `hcom-init.sh` + `hcom-backfill.sh --apply --archive`.

#### 1f. Regression Tests (12%)

```bash
bash "$HOME/.claude/scripts/infra-test.sh" --full 2>&1 | tail -1
# Parse: "Tests: N | Pass: P | Fail: F | Warn: W | Time: Ts"
# Score = P / N * 100
```

The regression component always runs `infra-test.sh --full` (depth is invariant).

#### 1h. Subsystems (v3, 11%)

Denomination-honest AW/PO model: only in-scope sub-checks count toward the denominator. Each sub-check has a real failing path (missing artifact / malformed JSON / broken smoke → 0 of its slice). All checks are read-only.

**Features scored** (all run every time — depth is invariant): the structural checks PLUS the py_compile / value-sanity / dead-url probes. The script-hygiene scan is the one exception, gated behind the orthogonal (non-depth) `--complete` flag.

| Feature | Structural | Deep extras (always run) |
|---------|-----------|----------------|
| Memory render/viewer pipeline | `render.py`, `viewer.py`, `comms_viewer.py` exist (8 pts) | `py_compile` each file (2 pts each) |
| Telemetry reader | `statusline_telemetry.py` + `.sh` exist (7 pts); `printf '{}'` smoke rc 0 + non-blank stdout (4 pts) | garbage-stdin smoke must also be rc 0 / fail-safe (3 pts) |
| Cost engine | `cost_cache_computer.py` + `cost-cache-refresh.sh` exist (6 pts); JSON schema: `.cost-cache.json` keys `day_usd`/`total_usd` (3 pts), `.cost-ledger.json` key `days` (3 pts), `.rate-latest.json` keys `five_hour_pct`/`seven_day_pct` (3 pts) | Value-sanity: pcts 0..100, all ledger day-costs ≥ 0, cache values ≥ 0 (3 pts) |
| nb-monitor | `nb-monitor.py` + `SKILL.md` exist (6 pts) | `py_compile` (2 pts) |
| latex-warn | `latex-warn.sh` exists (4 pts); no-op invocation rc 0 (3 pts) | — |
| figure-validate | `figure-validate.py` + `SKILL.md` exist (6 pts) | `py_compile` + `--help` rc 0 (3 pts) |
| experiment-harness | `experiment-harness.py` + `SKILL.md` exist (6 pts) | `py_compile` + `--help` rc 0 (3 pts) |
| better-super | `better_super_crawl.py`, `better_super_deps.py`, `dependencies.yml`, `SKILL.md` exist (6 pts) | `better-super-deps.sh --list` rc 0 (2 pts); dead-URL crawl must not produce `Traceback` (2 pts) |

**Graceful-absent / excluded**:

- `/super-mem` (`skills/super-mem/SKILL.md`) — GRACEFUL-ABSENT, deferred to Phase 8. Until the skill file lands it touches neither AW nor PO, so it neither penalizes nor inflates the score. Once the file exists, it becomes a real `ss_exists` sub-check.
- The deleted swarm/subagent monitor — EXCLUDED with no health score.

**`--complete` script-hygiene scan** (heavy, runs only when the orthogonal `--complete` flag is passed; NOT a depth selector): scans all `~/.claude/**/*.sh` for >2 consecutive lines of baked foreign-language (heredoc piped to python/node, multi-line `python3 -c`/`node -e` block). Each violating file deducts. `super-health.sh` itself is excluded from the scan. A clean fleet earns 6 pts.

**Score** = `AW * 100 / PO` (integer division, clamped 0..100). The denominator `PO` excludes any N/A/graceful-absent sub-check.

### Step 2: Calculate Aggregate Score

```
FINAL = (hook_score * 0.18) + (skill_score * 0.13) + (mem_score * 0.18)
      + (settings_score * 0.12) + (session_score * 0.05)
      + (comms_score * 0.10) + (regression_score * 0.12)
      + (subsystems_score * 0.12)
```

Round to nearest integer.

### Step 3: Grade

| Score | Grade | Meaning |
|-------|-------|---------|
| 90-100 | A | Production-ready |
| 80-89 | B | Healthy, minor issues |
| 70-79 | C | Functional, needs attention |
| 60-69 | D | Degraded, fix before expanding |
| <60 | F | Broken, immediate action needed |

### Step 4: Post-Hoc Audit (`--complete` tier only)

Dispatch 5 parallel `general-purpose` audit agents in a single message with multiple Agent tool calls. Each agent:
- Reads its assigned area read-only
- Categorizes findings as: **(a) Replication** — patterns/optims from this area applicable elsewhere, **(b) DRY/Atomization** — duplication, consolidation, simplification opportunities, **(c) ≥Medium ROI** — further optimization opportunities given current state and projects
- Caps output at 600 words

Audit areas (one agent each):
1. **Rules** (`~/.claude/rules/*.md`) — DRY across rule files, atomization candidates, cross-rule reference hygiene
2. **Agents** (`~/.claude/agents/*.md`) — model defaults compliance with `13-worker-first-mandate.md` matrix, frontmatter consistency, redundancy
3. **Skills** (`~/.claude/skills/*/SKILL.md`) — `/handoff` simplification post-swarm-first, overlap between delegation skills (`/autocommission`, `/swarm-dispatch`, `/topology-producer-reviewer`), other skills duplication
4. **Hooks** (`~/.claude/hooks/*.sh`) — similar-flow consolidation, efficiency wins, dead/superseded hooks
5. **Comms + Memory** (`~/.claude/comms/`, `~/.claude/agent-memory/`) — hygiene, cross-reference opportunities, dead entries, archival candidates

Synthesize the 5 reports into a prioritized optimization queue. Append to the standard `--deep` Output Format under a new section:

```markdown
### Post-Hoc Audit (--complete tier)

#### Replication Targets (a)
[priority-ranked list across all 5 audits]

#### DRY/Atomization Opportunities (b)
[priority-ranked list]

#### ≥Medium ROI Optimizations (c)
[priority-ranked list with effort estimates]
```

## Output Format

```
## Superclaude Health Report

**Score: NN/100 (Grade: X)** — tier: quick|standard|deep

### Component Scores
| Component | Weight | Score | Weighted |
|-----------|--------|-------|----------|
| Hook health | 18% | NN/100 | NN.N |
| Skill health | 13% | NN/100 | NN.N |
| Memory health | 18% | NN/100 | NN.N |
| Settings + agents | 12% | NN/100 | NN.N |
| Sessions | 5% | NN/100 | NN.N |
| Comms (HCOM broker) | 10% | NN/100 | NN.N |
| Regression tests | 12% | NN/100 | NN.N |
| Subsystems (v3) | 12% | NN/100 | NN.N |
| **Total** | **100%** | | **NN.N** |

### Top Issues (sorted by impact)
1. [highest-impact issue + which component]
2. [next issue]
3. ...

### Recommendations
- [prioritized action items]

### v3 Triggers (from /mem-health)
[forwarded from mem-health output]
```

## Constraints

- Each component produces an independent /100 score
- Final score is always weighted to /100
- Deterministic — same infrastructure state = same score
- `--deep` tier warns about session budget at start
- v3 triggers forwarded from /mem-health, never duplicated
