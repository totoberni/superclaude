---
name: hook-health
description: "Score hook subsystem health /100. Syntax, perf, naming, coverage."
category: health
user-invocable: true
disable-model-invocation: true
argument-hint: "[--quick | --standard | --deep] [hook-name | all]"
allowed-tools: Read, Bash, Glob, Grep, Agent
---

# Hook Health Assessment (/100)

Score the `~/.claude/hooks/` subsystem health. Read-only for `--quick`; `--standard`/`--deep` may spawn reviewers.

**Target**: $ARGUMENTS (default: `--quick all`)

## Scoring Criteria

| # | Criterion | Points | Measurement |
|---|-----------|--------|-------------|
| 1 | Syntax valid (`bash -n`) | 20 | All hooks + modules pass `bash -n`. Score = `passing / total * 20` |
| 2 | Permissions executable | 10 | All `.sh` files have `+x` bit. Score = `executable / total * 10` |
| 3 | Performance <500ms | 15 | Each hook completes in <500ms with mock input. Score = `fast / total * 15` |
| 4 | No hardcoded paths | 10 | Uses `$HOME`, `$TIMER_DIR`, not literal `$HOME/`. Score = `clean / total * 10` |
| 5 | Cleanup patterns complete | 10 | All temp file extensions covered in session-cleanup.sh. Score = `covered / total_temp_types * 10` |
| 6 | Module naming convention | 5 | Modules follow `NN-name.sh` pattern. Score = `compliant / total * 5` |
| 7 | Graceful degradation | 10 | Dispatcher handles missing modules (exits 0). Binary: 0 or 10 |
| 8 | Test coverage | 15 | Hooks/modules have test cases in test-hooks.sh. Score = `tested / total * 15` |
| 9 | No `set -e` in hooks | 5 | Binary: 0 (any `set -e` found in hooks/modules) or 5 |

## Tiers

| Tier | What It Does | Time |
|------|-------------|------|
| `--quick` | Script checks only (criteria 1-6, 9) | ~10 sec |
| `--standard` | Quick + spawn w-reviewer for design review (criteria 7-8) | ~5 min |
| `--deep` | Standard + spawn w-debugger for edge case testing | ~15 min |

## Procedure

### Quick Checks (all tiers)

#### 1. Syntax (20 pts)

```bash
HOOKS_DIR="$HOME/.claude/hooks"
for f in "$HOOKS_DIR"/*.sh "$HOOKS_DIR"/modules/*.sh; do
  [ -f "$f" ] && bash -n "$f" 2>&1
done
```

#### 2. Permissions (10 pts)

```bash
for f in "$HOOKS_DIR"/*.sh "$HOOKS_DIR"/modules/*.sh; do
  [ -f "$f" ] && [ -x "$f" ] && echo "OK: $(basename "$f")" || echo "MISS: $(basename "$f")"
done
```

#### 3. Performance (15 pts)

```bash
for f in "$HOOKS_DIR"/*.sh; do
  [ -f "$f" ] || continue
  START=$(date +%s%N)
  echo '{"session_id":"perf-test","tool_name":"Read"}' | timeout 1 bash "$f" > /dev/null 2>&1
  END=$(date +%s%N)
  MS=$(( (END - START) / 1000000 ))
  echo "$(basename "$f"): ${MS}ms"
done
```

Flag any hook >500ms.

#### 4. Hardcoded Paths (10 pts)

```bash
grep -rn "$HOME" "$HOOKS_DIR"/ 2>/dev/null | grep -v "^Binary"
```

Any match = that file scores 0 for this criterion.

#### 5. Cleanup Patterns (10 pts)

Known temp file types: `.start`, `.agent`, `.pid`, `.override`, `.context-warned`, `.tdd`, `.calls`

Check `session-cleanup.sh` covers all of these (both standalone `.ext` patterns and brace-expansion `{ext,...}` patterns):

```bash
TEMP_EXTS="start agent pid override context-warned tdd calls"
for ext in $TEMP_EXTS; do
  # Check both standalone ".ext" and brace-expansion "ext" (inside {})
  if grep -qE "\.$ext|[{,]${ext}([},])" "$HOOKS_DIR/session-cleanup.sh" 2>/dev/null; then
    echo "OK: $ext"
  else
    echo "MISS: $ext"
  fi
done
```

#### 6. Module Naming (5 pts)

Verify all files in `hooks/modules/` match `[0-9][0-9]-*.sh` pattern:

```bash
for f in "$HOOKS_DIR"/modules/*.sh; do
  basename "$f" | grep -qE '^[0-9]{2}-' || echo "BAD: $(basename "$f")"
done
```

#### 9. No set -e (5 pts)

```bash
grep -rn "set -e" "$HOOKS_DIR"/ 2>/dev/null | grep -v "set -euo" | grep -v "#"
# Also check for set -euo pipefail which includes -e
grep -rn "set -e" "$HOOKS_DIR"/ 2>/dev/null
```

Binary: any match in hooks/modules = 0 pts. (Note: `set -uo pipefail` without `-e` is OK in dispatcher.)

### Standard Checks (--standard, --deep)

#### 7. Graceful Degradation (10 pts)

Temporarily rename one module, run the dispatcher with mock input, verify exit 0. Rename back.

```bash
MOD=$(ls "$HOOKS_DIR"/modules/*.sh | head -1)
mv "$MOD" "${MOD}.bak"
echo '{"session_id":"degrade-test","tool_name":"Read"}' | bash "$HOOKS_DIR/session-timer.sh"
RESULT=$?
mv "${MOD}.bak" "$MOD"
# RESULT should be 0
```

Binary: exit 0 = 10 pts, non-zero = 0 pts.

#### 8. Test Coverage (15 pts)

Count hooks/modules referenced by name in test-hooks.sh:

```bash
# Count hooks/modules referenced by name in test-hooks.sh
TESTED=0
TOTAL=0
for f in "$HOOKS_DIR"/*.sh "$HOOKS_DIR"/modules/*.sh; do
  [ -f "$f" ] || continue
  TOTAL=$((TOTAL + 1))
  NAME=$(basename "$f" .sh)
  grep -q "$NAME" "$HOME/.claude/scripts/test-hooks.sh" 2>/dev/null && TESTED=$((TESTED + 1))
done
echo "Coverage: $TESTED / $TOTAL"
```

Score = TESTED / TOTAL * 15.

### Deep Checks (--deep only)

Spawn `w-debugger` subagent to test:
- Missing `$TIMER_DIR` directory
- Corrupted `.start` files (non-numeric content)
- Concurrent hook execution (two mock inputs in parallel)
- Empty JSON input to dispatcher

Warn at start: "Deep hook health takes ~15 min. Session budget impact."

## Output Format

```
## Hook Health Report

**Score: NN/100** (tier: quick|standard|deep)

### Criteria Breakdown
| # | Criterion | Score | Detail |
|---|-----------|-------|--------|
| 1 | Syntax valid | NN/20 | X/Y pass bash -n |
| 2 | Permissions | NN/10 | X/Y executable |
| 3 | Performance | NN/15 | X/Y under 500ms |
| 4 | No hardcoded paths | NN/10 | X files clean |
| 5 | Cleanup patterns | NN/10 | X/Y temp types covered |
| 6 | Module naming | NN/5 | X/Y compliant |
| 7 | Graceful degradation | NN/10 | pass/fail (standard+ only) |
| 8 | Test coverage | NN/15 | X/Y tested (standard+ only) |
| 9 | No set -e | NN/5 | pass/fail |

### Issues Found
- [list of specific issues, if any]

### Recommendations
- [actionable fixes]
```
