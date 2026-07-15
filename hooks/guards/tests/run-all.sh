#!/usr/bin/env bash
# ~/.claude/hooks/guards/tests/run-all.sh — bite-test battery (PHASE2-CONTRACT sec 6).
# Runs every test-*.sh in this dir, prints a per-file summary, exits non-zero if any
# fail. This runner IS the Phase-3 bite-test arm of the seal.

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
total=0
fails=0

shopt -s nullglob
for t in "$TESTDIR"/test-*.sh; do
  total=$((total + 1))
  if bash "$t"; then
    echo ">>> $(basename "$t"): PASS"
  else
    echo ">>> $(basename "$t"): FAIL"
    fails=$((fails + 1))
  fi
  echo
done

if [ "$total" -eq 0 ]; then
  echo "run-all: no test-*.sh found in $TESTDIR"
  exit 1
fi

echo "================================================"
echo "run-all SUMMARY: $((total - fails))/$total test file(s) passed"
echo "================================================"
[ "$fails" -eq 0 ]
