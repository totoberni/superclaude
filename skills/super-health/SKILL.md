---
name: super-health
description: "Aggregate superclaude health /100. Hooks, skills, mem, settings, sessions."
category: health
user-invocable: true
disable-model-invocation: true
argument-hint: "[--quick | --standard | --deep]"
allowed-tools: Read, Bash, Glob, Grep, Agent
---

# Superclaude Health Assessment (/100)

Aggregate health score across all superclaude subsystems. Weighted combination of component scores.

**Tier**: $ARGUMENTS (default: `--quick`)

## Component Weights

| Component | Weight | Source Skill/Script |
|-----------|--------|---------------------|
| Hook health | 20% | `/hook-health` |
| Skill health | 15% | `/skill-health` |
| Memory health | 20% | `/mem-health` |
| Settings + agents | 20% | Inline checks (below) |
| Sessions | 10% | Inline checks (below) |
| Regression tests | 15% | `infra-test.sh` |

**Final score** = sum of `component_score * weight` across all 6 components.

## Tiers

| Tier | Components | Time |
|------|-----------|------|
| `--quick` | hook-health `--quick` + skill-health + mem-health + settings/agents + sessions + infra-test.sh | ~2 min |
| `--standard` | hook-health `--standard` + all quick checks | ~10 min |
| `--deep` | hook-health `--deep` + infra-test.sh `--full` + all standard checks. **Warn about session budget** | ~20 min |

## Procedure

### Step 1: Run Component Health Checks

Execute each component and capture its /100 score.

#### 1a. Hook Health (20%)

Run `/hook-health` at the appropriate tier. Capture the `/100` score.

#### 1b. Skill Health (15%)

Run `/skill-health all`. Capture the `/100` score.

#### 1c. Memory Health (20%)

Run `/mem-health`. Capture the `/100` score.

#### 1d. Settings + Agents (20%)

Inline scoring — no separate skill needed:

| Criterion | Points | Check |
|-----------|--------|-------|
| settings.json valid JSON | 25 | `jq . ~/.claude/settings.json > /dev/null 2>&1` |
| Agent frontmatter valid | 25 | All agents have `---`, `name:`, `model:` |
| Model values valid | 15 | All `model:` values are `sonnet`, `opus`, `haiku`, or `[1m]` variant (e.g. `opus[1m]`) |
| Deny rules >= 5 | 15 | `jq '.permissions.deny \| length' ~/.claude/settings.json` |
| No orphan agents | 10 | Agent file without matching comms dir (excluding workers/base) |
| No broken symlinks | 10 | `find ~/.claude/agents/ -maxdepth 1 -type l ! -exec test -e {} \; -print` |

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

#### 1e. Sessions (10%)

Inline scoring:

| Criterion | Points | Check |
|-----------|--------|-------|
| No zombie timer files | 50 | PID dead but .start/.agent exist |
| No orphaned session files | 30 | .start without .agent or vice versa |
| Active sessions within limits | 20 | All active sessions <48 min |

```bash
TIMER_DIR="$HOME/.claude/session-timers"
SCORE=0

if [ ! -d "$TIMER_DIR" ]; then
  echo "Sessions: 100/100 (no timer dir = clean)"
else
  # No zombies (50 pts)
  ZOMBIES=0
  for pf in "$TIMER_DIR"/*.pid; do
    [ -f "$pf" ] || continue
    PID=$(cat "$pf" 2>/dev/null)
    [ -n "$PID" ] && ! kill -0 "$PID" 2>/dev/null && ZOMBIES=$((ZOMBIES + 1))
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

  # Within limits (20 pts)
  OVER=0
  NOW=$(date +%s)
  for sf in "$TIMER_DIR"/*.start; do
    [ -f "$sf" ] || continue
    START=$(cat "$sf" 2>/dev/null)
    [[ "$START" =~ ^[0-9]+$ ]] || continue
    AGE_MIN=$(( (NOW - START) / 60 ))
    [ "$AGE_MIN" -gt 48 ] && OVER=$((OVER + 1))
  done
  [ "$OVER" -eq 0 ] && SCORE=$((SCORE + 20))

  echo "Sessions: $SCORE/100"
fi
```

#### 1f. Regression Tests (15%)

```bash
bash "$HOME/.claude/scripts/infra-test.sh" --full 2>&1 | tail -1
# Parse: "Tests: N | Pass: P | Fail: F | Warn: W | Time: Ts"
# Score = P / N * 100
```

For `--quick` tier, use `infra-test.sh` without `--full` if available, or just count pass rate.

### Step 2: Calculate Aggregate Score

```
FINAL = (hook_score * 0.20) + (skill_score * 0.15) + (mem_score * 0.20)
      + (settings_score * 0.20) + (session_score * 0.10) + (regression_score * 0.15)
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

## Output Format

```
## Superclaude Health Report

**Score: NN/100 (Grade: X)** — tier: quick|standard|deep

### Component Scores
| Component | Weight | Score | Weighted |
|-----------|--------|-------|----------|
| Hook health | 20% | NN/100 | NN.N |
| Skill health | 15% | NN/100 | NN.N |
| Memory health | 20% | NN/100 | NN.N |
| Settings + agents | 20% | NN/100 | NN.N |
| Sessions | 10% | NN/100 | NN.N |
| Regression tests | 15% | NN/100 | NN.N |
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
