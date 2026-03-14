---
name: skill-health
description: "Score skill subsystem health /100. Frontmatter, refs, descriptions."
category: health
user-invocable: true
disable-model-invocation: true
argument-hint: "[skill-name | all]"
allowed-tools: Read, Bash, Glob, Grep
---

# Skill Health Assessment (/100)

Score the `~/.claude/skills/` subsystem health. Read-only assessment.

**Target**: $ARGUMENTS (default: all)

## Scoring Criteria

| # | Criterion | Points | Measurement |
|---|-----------|--------|-------------|
| 1 | Valid frontmatter | 20 | All SKILL.md have name, description, user-invocable. Score = `valid / total * 20` |
| 2 | Category field present | 10 | All skills have `category:` in frontmatter. Score = `categorized / total * 10` |
| 3 | No duplicate names | 10 | No two skills share a `name:` value. Binary: 0 (any dupe) or 10 |
| 4 | Referenced scripts exist | 15 | All paths/scripts in skill body exist on disk. Score = `valid_refs / total_refs * 15` |
| 5 | No dead skills | 10 | Skills not user-invocable AND not referenced by any agent. Score = `(total - dead) / total * 10` |
| 6 | Description <=80 chars | 10 | Short descriptions reduce context. Score = `short / total * 10` |
| 7 | Valid agent references | 10 | Skills referencing agent types reference existing agent files. Score = `valid / total * 10` |
| 8 | Usage examples present | 15 | User-invocable skills have examples in body. Score = `documented / user_invocable * 15` |

## Procedure

### 1. Valid Frontmatter (20 pts)

For each `skills/*/SKILL.md`:

```bash
SKILLS_DIR="$HOME/.claude/skills"
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  FIRST=$(head -1 "$f")
  if [ "$FIRST" != "---" ]; then
    echo "FAIL: $(basename "$d") — no frontmatter"
    continue
  fi
  # Check required fields
  FM=$(sed -n '2,/^---$/p' "$f" | head -20)
  for field in name description user-invocable; do
    echo "$FM" | grep -q "^${field}:" || echo "MISS: $(basename "$d") — $field"
  done
done
```

### 2. Category Field (10 pts)

Check each skill's frontmatter for `category:` field.

```bash
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  FM=$(sed -n '2,/^---$/p' "$f" | head -20)
  echo "$FM" | grep -q "^category:" || echo "MISS: $(basename "$d")"
done
```

Note: category field comes from DIR-042. If not yet implemented, score full 10 and note "category not yet required (DIR-042 pending)".

### 3. No Duplicate Names (10 pts)

```bash
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  sed -n 's/^name: *//p' "$f"
done | sort | uniq -d
```

Any output = 0 pts.

### 4. Referenced Scripts Exist (15 pts)

Scan skill bodies for backtick-quoted paths and `$HOME`/`~/.claude` references:

```bash
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  # Extract paths like ~/.claude/..., $HOME/.claude/..., scripts/*.sh
  grep -oP '(?:`|")((?:\$HOME|~)/\.claude/[^`"]+)(?:`|")' "$f" 2>/dev/null | tr -d '`"'
done | sort -u | while read -r p; do
  expanded=$(echo "$p" | sed "s|~|$HOME|;s|\\\$HOME|$HOME|")
  [ -e "$expanded" ] || echo "BROKEN: $p"
done
```

Score = `(total_refs - broken) / total_refs * 15`. Full 15 if no refs found.

### 5. No Dead Skills (10 pts)

A skill is "dead" if:
- `user-invocable: false` (or missing) AND
- Not referenced by any agent definition in `~/.claude/agents/*.md`

```bash
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  NAME=$(basename "$d")
  UI=$(sed -n 's/^user-invocable: *//p' "$f" | tr -d ' ')
  if [ "$UI" != "true" ]; then
    # Check if any agent references this skill
    grep -rlq "$NAME" "$HOME/.claude/agents/"*.md 2>/dev/null || echo "DEAD: $NAME"
  fi
done
```

### 6. Description Length (10 pts)

```bash
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  DESC=$(sed -n 's/^description: *"\(.*\)"/\1/p' "$f" | head -1)
  LEN=${#DESC}
  [ "$LEN" -gt 80 ] && echo "LONG: $(basename "$d") ($LEN chars)"
done
```

### 7. Valid Agent References (10 pts)

Skills that mention agent types (e.g., "spawn w-reviewer", "use w-debugger"):

```bash
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  # Find references to w-* or agent names
  grep -oP 'w-\w+|(?:spawn|use|delegate to) (\w+)' "$f" 2>/dev/null | while read -r ref; do
    agent=$(echo "$ref" | grep -oP 'w-\w+' || echo "")
    [ -z "$agent" ] && continue
    [ -f "$HOME/.claude/agents/${agent}.md" ] || echo "BROKEN: $(basename "$d") -> $agent"
  done
done
```

Full 10 if no agent references found.

### 8. Usage Examples (15 pts)

User-invocable skills should have at least one usage example or procedure section:

```bash
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  UI=$(sed -n 's/^user-invocable: *//p' "$f" | tr -d ' ')
  [ "$UI" = "true" ] || continue
  # Check for example/usage/procedure sections or code blocks
  if grep -qiE '(## (Usage|Example|Procedure|How|Step)|```|^\$ARGUMENTS)' "$f" 2>/dev/null; then
    : # has examples
  else
    echo "NO_EXAMPLES: $(basename "$d")"
  fi
done
```

## Output Format

```
## Skill Health Report

**Score: NN/100**

### Criteria Breakdown
| # | Criterion | Score | Detail |
|---|-----------|-------|--------|
| 1 | Valid frontmatter | NN/20 | X/Y valid |
| 2 | Category field | NN/10 | X/Y categorized |
| 3 | No duplicate names | NN/10 | pass/fail |
| 4 | Referenced scripts | NN/15 | X broken refs |
| 5 | No dead skills | NN/10 | X dead skills |
| 6 | Description <=80ch | NN/10 | X/Y under limit |
| 7 | Agent references | NN/10 | X broken refs |
| 8 | Usage examples | NN/15 | X/Y documented |

### Issues Found
- [list of specific issues, if any]

### Recommendations
- [actionable fixes]
```
