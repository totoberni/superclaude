#!/usr/bin/env bash
# decontaminate.sh - Superclaude to Local Codebase Firewall grep check.
#
# Extracted from:
#   - skills/research/SKILL.md, "Cross-subcommand conventions" Decontamination
#     block (source of the ~/.claude/ exemption rule below).
#   - rules/20-tool-conventions.md, "Superclaude <-> Local Codebase Firewall"
#     section (the more complete forbidden-pattern list; research's own list
#     is a subset of this one, so the union below is effectively this list).
#
# Grep-based only; this script never rewrites or strips matches, it only
# reports them. Per rules/20-tool-conventions.md the actual stripping step
# ("grep the draft for the forbidden patterns and strip them") stays a
# manual or caller-side action.
#
# `\d+` in the source prose is translated to the POSIX ERE `[0-9]+` so the
# patterns run with plain `grep -E` (no PCRE dependency required).
#
# Two source bullets are prose-only (no concrete regex given) and are NOT
# implemented as literal patterns: "any project memory filename" (the
# rules-file example repeats an identical placeholder twice, so there is no
# fixed literal to extract) and "internal orch names, internal
# project-memory filenames". These require semantic judgment beyond grep.
#
# Known limitation inherited from the source: the identifier patterns
# (M-[0-9]+ etc.) match as plain substrings, so an unrelated token that
# happens to end the same way (for example an identifier ending "M-5") can
# false-positive. The source material itself expects a human or caller to
# triage hits rather than treat every match as certain contamination.
#
# Usage:
#   decontaminate.sh <file>...
#
# Exit 0: no forbidden pattern found in any checked file (or all files were
#         exempt because they live under ~/.claude/).
# Exit 1: at least one file has a hit; a per-file hit list is printed.

if [ "$#" -eq 0 ]; then
  echo "Usage: decontaminate.sh <file>..."
  exit 1
fi

PATTERNS='~/\.claude/|\.claude/rules|agent-memory|shared/projects/|class/meta|MEMORY\.md|mtm\.md|ltm\.md|\.memory\.db|\.comms\.db|\.broker\.db|memory_db\.py|comms_db\.py|M-[0-9]+|MM-[0-9]+|GM-[0-9]+|G-[0-9]+|MT-[0-9]+|CW-[0-9]+|W-[0-9]+|meta says|memory\.md says|according to the gotchas file|see the project memory'

HOME_CLAUDE="$HOME/.claude/"
FOUND=0

for f in "$@"; do
  [ -f "$f" ] || { echo "== $f (not found, skipped) =="; continue; }

  ABS=$(readlink -f "$f" 2>/dev/null)
  [ -z "$ABS" ] && ABS="$f"

  case "$ABS" in
    "$HOME_CLAUDE"*)
      # Paths under ~/.claude/ are exempt from decontamination, per
      # skills/research/SKILL.md Decontamination block.
      continue
      ;;
  esac

  HITS=$(grep -n -E "$PATTERNS" "$f")
  if [ -n "$HITS" ]; then
    echo "== $f =="
    echo "$HITS"
    FOUND=1
  fi
done

if [ "$FOUND" -eq 1 ]; then
  exit 1
fi

echo "clean"
exit 0
