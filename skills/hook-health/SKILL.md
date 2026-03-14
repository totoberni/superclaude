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

Score `~/.claude/hooks/` health. Read-only for `--quick`; `--standard`/`--deep` spawn reviewers.

**Target**: $ARGUMENTS (default: `--quick all`)

## Scoring Criteria

| # | Criterion | Pts | Tiers | Measurement |
|---|-----------|-----|-------|-------------|
| 1 | Syntax (`bash -n`) | 20 | all | `passing / total * 20` |
| 2 | Permissions (`+x`) | 10 | all | `executable / total * 10` |
| 3 | Performance <500ms | 15 | all | `fast / total * 15` |
| 4 | No hardcoded paths | 10 | all | `clean / total * 10` |
| 5 | Cleanup patterns | 10 | all | `covered / total_temp_types * 10` |
| 6 | Module naming `NN-*.sh` | 5 | all | `compliant / total * 5` |
| 7 | Graceful degradation | 10 | std+ | Binary: dispatcher exit 0 with missing module |
| 8 | Test coverage | 15 | std+ | `tested / total * 15` (refs in test-hooks.sh) |
| 9 | No `set -e` | 5 | all | Binary: any match = 0 |

## Quick Checks (all tiers)

```bash
HOOKS_DIR="$HOME/.claude/hooks"
# 1. Syntax
for f in "$HOOKS_DIR"/*.sh "$HOOKS_DIR"/modules/*.sh; do [ -f "$f" ] && bash -n "$f" 2>&1; done
# 2. Permissions
for f in "$HOOKS_DIR"/*.sh "$HOOKS_DIR"/modules/*.sh; do [ -f "$f" ] && ([ -x "$f" ] && echo "OK: $(basename "$f")" || echo "MISS: $(basename "$f")"); done
# 3. Performance (test dispatchers registered in settings.json, NOT pre-tool-use.sh)
for f in "$HOOKS_DIR"/*.sh; do [ -f "$f" ] || continue; START=$(date +%s%N); echo '{"session_id":"perf-test","tool_name":"Read"}' | timeout 1 bash "$f" >/dev/null 2>&1; END=$(date +%s%N); echo "$(basename "$f"): $(( (END-START)/1000000 ))ms"; done
# 4. Hardcoded paths
grep -rn "$HOME" "$HOOKS_DIR"/ 2>/dev/null | grep -v "^Binary"
# 5. Cleanup patterns (temp exts: start agent pid override context-warned tdd calls)
for ext in start agent pid override context-warned tdd calls; do grep -qE "\.$ext|[{,]${ext}([},])" "$HOOKS_DIR/session-cleanup.sh" 2>/dev/null && echo "OK: $ext" || echo "MISS: $ext"; done
# 6. Module naming
for f in "$HOOKS_DIR"/modules/*.sh; do basename "$f" | grep -qE '^[0-9]{2}-' || echo "BAD: $(basename "$f")"; done
# 9. No set -e
grep -rn "set -e" "$HOOKS_DIR"/ 2>/dev/null
```

## Standard Checks (--standard, --deep)

**7. Graceful degradation**: Rename one module → run dispatcher (`session-timer.sh`) with mock JSON → verify exit 0 → rename back. Note: the dispatcher is registered in `settings.json` under `PreToolUse` — do NOT hardcode `pre-tool-use.sh` (it doesn't exist).

**8. Test coverage**: Count hooks/modules referenced by name in `~/.claude/scripts/test-hooks.sh`. Score = tested/total * 15.

## Deep Checks (--deep only)

Spawn `w-debugger` to test: missing `$TIMER_DIR`, corrupted `.start` files, concurrent execution, empty JSON input. Warn about session budget (~15 min).

## Output

```
## Hook Health Report
**Score: NN/100** (tier: quick|standard|deep)
| # | Criterion | Score | Detail |
|---|-----------|-------|--------|
### Issues Found
### Recommendations
```
