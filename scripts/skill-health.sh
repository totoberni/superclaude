#!/bin/bash
# skill-health.sh — deterministic implementation of the /skill-health rubric.
# Canonical runner behind skills/skill-health/SKILL.md (8 weighted criteria, 100 pts).
#
# Usage:
#   bash skill-health.sh [skill-name|all]   (scope arg accepted but scoring is subsystem-wide)
#
# Contract: prints a per-criterion breakdown, and as its FINAL stdout line,
# exactly: "SCORE: <int>/100".  Exit code is ALWAYS 0 (reporting tool, not a gate).
#
# Determinism: same skills tree => identical score.
# Defensive: a missing file/dir yields 0 for THAT check (noted), never a crash.

set -uo pipefail   # NEVER set -e

CLAUDE="${CLAUDE_DIR:-$HOME/.claude}"
SKILLS_DIR="$CLAUDE/skills"
AGENT_DIR="$CLAUDE/agents"

# pct_points <ok> <total> <max> -> integer points (floor); 0 if total==0
pct_points() {
  local ok="$1" total="$2" max="$3"
  [ "$total" -gt 0 ] 2>/dev/null || { echo 0; return; }
  echo $(( ok * max / total ))
}

SCORE=0
echo "## Skill Health Report"
echo ""
echo "| # | Criterion | Score | Detail |"
echo "|---|-----------|-------|--------|"

# ─────────────────────────────────────────────────────────────
# Criterion 1: Valid frontmatter — 20 pts — valid/total*20
#   A skill is valid iff first line is '---' AND frontmatter has
#   name:, description:, user-invocable:.
# ─────────────────────────────────────────────────────────────
c1_total=0; c1_ok=0; c1_bad=""
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  c1_total=$((c1_total + 1))
  name=$(basename "$d")
  if [ "$(head -1 "$f")" != "---" ]; then
    c1_bad="$c1_bad $name(no-fm)"
    continue
  fi
  fm=$(sed -n '2,/^---$/p' "$f" | head -30)
  miss=""
  for field in name description user-invocable; do
    echo "$fm" | grep -q "^${field}:" || miss="${miss}${field},"
  done
  if [ -z "$miss" ]; then
    c1_ok=$((c1_ok + 1))
  else
    c1_bad="$c1_bad $name(${miss%,})"
  fi
done
c1=$(pct_points "$c1_ok" "$c1_total" 20)
SCORE=$((SCORE + c1))
echo "| 1 | Valid frontmatter | $c1/20 | $c1_ok/$c1_total valid${c1_bad:+; bad:$c1_bad} |"

# ─────────────────────────────────────────────────────────────
# Criterion 2: Category field present — 10 pts — categorized/total*10
# ─────────────────────────────────────────────────────────────
c2_total=0; c2_ok=0; c2_miss=""
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  c2_total=$((c2_total + 1))
  fm=$(sed -n '2,/^---$/p' "$f" | head -30)
  if echo "$fm" | grep -q "^category:"; then
    c2_ok=$((c2_ok + 1))
  else
    c2_miss="$c2_miss $(basename "$d")"
  fi
done
c2=$(pct_points "$c2_ok" "$c2_total" 10)
SCORE=$((SCORE + c2))
echo "| 2 | Category field | $c2/10 | $c2_ok/$c2_total categorized (missing:${c2_miss:- none}) |"

# ─────────────────────────────────────────────────────────────
# Criterion 3: No duplicate names — 10 pts — BINARY (any dupe => 0)
# ─────────────────────────────────────────────────────────────
dupes=$(for d in "$SKILLS_DIR"/*/; do
          f="$d/SKILL.md"; [ -f "$f" ] || continue
          sed -n 's/^name: *//p' "$f" | head -1
        done | sort | uniq -d)
if [ -z "$dupes" ]; then
  c3=10; c3_detail="no duplicates"
else
  c3=0; c3_detail="duplicates: $(echo "$dupes" | tr '\n' ' ')"
fi
SCORE=$((SCORE + c3))
echo "| 3 | No duplicate names | $c3/10 | $c3_detail |"

# ─────────────────────────────────────────────────────────────
# Criterion 4: Referenced scripts exist — 15 pts — (total-broken)/total*15
#   Extract backtick/quote-wrapped ~/.claude or $HOME/.claude paths from each body,
#   strip template/runtime patterns, verify on disk. Full 15 if no refs.
# ─────────────────────────────────────────────────────────────
c4_refs=0; c4_broken=0; c4_broken_list=""
refs=$(for d in "$SKILLS_DIR"/*/; do
         f="$d/SKILL.md"; [ -f "$f" ] || continue
         grep -oP '(?:`|")((?:\$HOME|~)/\.claude/[^\s`"]+)(?:`|")' "$f" 2>/dev/null | tr -d '`"'
       done | sort -u | grep -vE '<[^>]+>|\$ARGUMENTS|\$\{|\*|^--|checkpoint|alert|_pending_promotion|nb-progress')
while IFS= read -r p; do
  [ -n "$p" ] || continue
  c4_refs=$((c4_refs + 1))
  expanded=$(echo "$p" | sed "s|~|$HOME|;s|\\\$HOME|$HOME|")
  if [ ! -e "$expanded" ]; then
    c4_broken=$((c4_broken + 1))
    c4_broken_list="$c4_broken_list $p"
  fi
done <<< "$refs"
if [ "$c4_refs" -eq 0 ]; then
  c4=15; c4_detail="no refs found (full)"
else
  c4=$(pct_points "$(( c4_refs - c4_broken ))" "$c4_refs" 15)
  c4_detail="$(( c4_refs - c4_broken ))/$c4_refs valid${c4_broken_list:+; broken:$c4_broken_list}"
fi
SCORE=$((SCORE + c4))
echo "| 4 | Referenced scripts exist | $c4/15 | $c4_detail |"

# ─────────────────────────────────────────────────────────────
# Criterion 5: No dead skills — 10 pts — (total-dead)/total*10
#   dead = user-invocable != true AND not referenced by any agent .md
# ─────────────────────────────────────────────────────────────
c5_total=0; c5_dead=0; c5_dead_list=""
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  c5_total=$((c5_total + 1))
  name=$(basename "$d")
  ui=$(sed -n 's/^user-invocable: *//p' "$f" | head -1 | tr -d ' ')
  if [ "$ui" != "true" ]; then
    if ! grep -rlq "$name" "$AGENT_DIR"/*.md 2>/dev/null; then
      c5_dead=$((c5_dead + 1))
      c5_dead_list="$c5_dead_list $name"
    fi
  fi
done
c5=$(pct_points "$(( c5_total - c5_dead ))" "$c5_total" 10)
SCORE=$((SCORE + c5))
echo "| 5 | No dead skills | $c5/10 | $c5_dead dead${c5_dead_list:+:$c5_dead_list} |"

# ─────────────────────────────────────────────────────────────
# Criterion 6: Description <= 80 chars — 10 pts — short/total*10
# ─────────────────────────────────────────────────────────────
c6_total=0; c6_ok=0; c6_long=""
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  c6_total=$((c6_total + 1))
  desc=$(sed -n 's/^description: *"\(.*\)"/\1/p' "$f" | head -1)
  # Fall back to unquoted description if the quoted form did not match.
  [ -z "$desc" ] && desc=$(sed -n 's/^description: *//p' "$f" | head -1 | sed 's/^"//;s/"$//')
  len=${#desc}
  if [ "$len" -le 80 ]; then
    c6_ok=$((c6_ok + 1))
  else
    c6_long="$c6_long $(basename "$d")(${len})"
  fi
done
c6=$(pct_points "$c6_ok" "$c6_total" 10)
SCORE=$((SCORE + c6))
echo "| 6 | Description <=80ch | $c6/10 | $c6_ok/$c6_total under limit (long:${c6_long:- none}) |"

# ─────────────────────────────────────────────────────────────
# Criterion 7: Valid agent references — 10 pts — valid/total*10
#   For every \bw-<name> token in a body, the agent file must exist.
#   Full 10 if no agent references found.
# ─────────────────────────────────────────────────────────────
c7_refs=0; c7_broken=0; c7_broken_list=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  skilldir="${line%%::*}"
  agent="${line##*::}"
  c7_refs=$((c7_refs + 1))
  if [ ! -f "$AGENT_DIR/${agent}.md" ]; then
    c7_broken=$((c7_broken + 1))
    c7_broken_list="$c7_broken_list $skilldir->$agent"
  fi
done < <(
  for d in "$SKILLS_DIR"/*/; do
    f="$d/SKILL.md"; [ -f "$f" ] || continue
    sd=$(basename "$d")
    # Exclude false-positives: <...> templates, w-eph* (ephemeral — transient by
    # design, never permanent files), and documented non-agent terms w-swarm (the
    # Meta+w-swarm pattern) + w-rewriter (a promote example).
    sed 's/<[^>]*>/ /g' "$f" 2>/dev/null | grep -oP '\bw-[\w-]+' | sort -u \
      | grep -vE '^w-eph|^w-swarm$|^w-rewriter$' | while read -r agent; do
      echo "${sd}::${agent}"
    done
  done
)
if [ "$c7_refs" -eq 0 ]; then
  c7=10; c7_detail="no agent refs (full)"
else
  c7=$(pct_points "$(( c7_refs - c7_broken ))" "$c7_refs" 10)
  c7_detail="$(( c7_refs - c7_broken ))/$c7_refs valid${c7_broken_list:+; broken:$c7_broken_list}"
fi
SCORE=$((SCORE + c7))
echo "| 7 | Agent references | $c7/10 | $c7_detail |"

# ─────────────────────────────────────────────────────────────
# Criterion 8: Usage examples present — 15 pts — documented/user_invocable*15
#   For each user-invocable skill, body must contain a section header / code
#   fence / **Args**:/ **Usage**:/ $ARGUMENTS marker.
# ─────────────────────────────────────────────────────────────
c8_ui=0; c8_ok=0; c8_missing=""
for d in "$SKILLS_DIR"/*/; do
  f="$d/SKILL.md"
  [ -f "$f" ] || continue
  ui=$(sed -n 's/^user-invocable: *//p' "$f" | head -1 | tr -d ' ')
  [ "$ui" = "true" ] || continue
  c8_ui=$((c8_ui + 1))
  if grep -qiE '(^## [A-Za-z]|```|\*\*Args\*\*:|\*\*Usage\*\*:|\$ARGUMENTS)' "$f" 2>/dev/null; then
    c8_ok=$((c8_ok + 1))
  else
    c8_missing="$c8_missing $(basename "$d")"
  fi
done
if [ "$c8_ui" -eq 0 ]; then
  c8=15; c8_detail="no user-invocable skills (full)"
else
  c8=$(pct_points "$c8_ok" "$c8_ui" 15)
  c8_detail="$c8_ok/$c8_ui documented (missing:${c8_missing:- none})"
fi
SCORE=$((SCORE + c8))
echo "| 8 | Usage examples | $c8/15 | $c8_detail |"

echo ""
[ "$SCORE" -lt 0 ] && SCORE=0
[ "$SCORE" -gt 100 ] && SCORE=100
echo "SCORE: $SCORE/100"
exit 0
