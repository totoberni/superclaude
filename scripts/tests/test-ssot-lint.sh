#!/usr/bin/env bash
# Bite-tests for ssot-lint.py. Self-contained: builds fixture trees under mktemp -d (never
# touches ~/.claude). Prints PASS/FAIL per case; exits non-zero if any case fails.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINT="$SCRIPT_DIR/../ssot-lint.py"

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

# make_clean_fixture <dir> : a minimal, fully-clean corpus + registry. Every case below starts
# from a fresh copy of this so mutations in one case cannot bleed into another.
make_clean_fixture() {
  local dir="$1"
  mkdir -p "$dir/rules" "$dir/skills/_shared" "$dir/agents" "$dir/docs" "$dir/meta"

  cat > "$dir/CLAUDE.md" <<'EOF'
# Fixture root

See `rules/13-worker-first-mandate.md` for the thinking doctrine.
EOF

  cat > "$dir/rules/13-worker-first-mandate.md" <<'EOF'
# Worker First Mandate (fixture)

## Critical Note

Thinking is inherited from the session in this fixture doctrine.
EOF

  cat > "$dir/skills/_shared/verdict-schema.md" <<'EOF'
# verdict schema (fixture)

Nothing interesting here.
EOF

  cat > "$dir/agents/orch.md" <<'EOF'
# orch (fixture)

Pointer only: see `rules/13-worker-first-mandate.md` § Critical Note.
SOT: `skills/_shared/verdict-schema.md`
EOF

  cat > "$dir/docs/notes.md" <<'EOF'
# docs notes (fixture)

Nothing load-bearing.
EOF

  cat > "$dir/meta/concept-registry.yaml" <<EOF
- id: test-thinking-doctrine
  canonical_home: rules/13-worker-first-mandate.md:Critical Note
  defining_marker: '^##\s+Critical Note\$'
  forbidden_pattern: 'thinking is NOT inherited \(retired\)'

- id: test-cwd-fact
  canonical_home: CLAUDE.md
  ground_truth: 'test -d $dir'
EOF
}

# -----------------------------------------------------------------------------------------
# (a) clean fixture -> exit 0
# -----------------------------------------------------------------------------------------
run_case_a() {
  local dir
  dir="$(mktemp -d)"
  make_clean_fixture "$dir"
  local out
  out="$(python3 "$LINT" --root "$dir" 2>&1)"
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    pass "(a) clean fixture exits 0"
  else
    fail "(a) clean fixture expected exit 0, got $rc -- output:
$out"
  fi
  rm -rf "$dir"
}

# -----------------------------------------------------------------------------------------
# (b) a defining_marker re-defined in a second file -> exit non-zero, names that file
# -----------------------------------------------------------------------------------------
run_case_b() {
  local dir
  dir="$(mktemp -d)"
  make_clean_fixture "$dir"
  # Inject a second literal "## Critical Note" heading outside the canonical_home.
  cat >> "$dir/agents/orch.md" <<'EOF'

## Critical Note

A second, competing definition landed here by mistake.
EOF
  local out
  out="$(python3 "$LINT" --root "$dir" 2>&1)"
  local rc=$?
  if [ "$rc" -ne 0 ] && echo "$out" | grep -q "re-defined outside canonical_home: agents/orch.md"; then
    pass "(b) re-defined marker fails and names agents/orch.md"
  else
    fail "(b) expected non-zero exit naming agents/orch.md -- got rc=$rc, output:
$out"
  fi
  rm -rf "$dir"
}

# -----------------------------------------------------------------------------------------
# (c) a forbidden_pattern hit outside the home -> exit non-zero
# -----------------------------------------------------------------------------------------
run_case_c() {
  local dir
  dir="$(mktemp -d)"
  make_clean_fixture "$dir"
  cat >> "$dir/docs/notes.md" <<'EOF'

Someone wrote: thinking is NOT inherited (retired) in the wrong place.
EOF
  local out
  out="$(python3 "$LINT" --root "$dir" 2>&1)"
  local rc=$?
  if [ "$rc" -ne 0 ] && echo "$out" | grep -q "forbidden_pattern hit outside canonical_home: docs/notes.md"; then
    pass "(c) forbidden_pattern hit outside home fails and names docs/notes.md"
  else
    fail "(c) expected non-zero exit naming docs/notes.md -- got rc=$rc, output:
$out"
  fi
  rm -rf "$dir"
}

# -----------------------------------------------------------------------------------------
# (d) a broken 'SOT: <nonexistent path>' pointer -> exit non-zero
# -----------------------------------------------------------------------------------------
run_case_d() {
  local dir
  dir="$(mktemp -d)"
  make_clean_fixture "$dir"
  cat >> "$dir/docs/notes.md" <<'EOF'

SOT: `rules/does-not-exist.md`
EOF
  local out
  out="$(python3 "$LINT" --root "$dir" 2>&1)"
  local rc=$?
  if [ "$rc" -ne 0 ] && echo "$out" | grep -q "unresolved target 'rules/does-not-exist.md'"; then
    pass "(d) broken SOT pointer fails and names the unresolved target"
  else
    fail "(d) expected non-zero exit naming the unresolved target -- got rc=$rc, output:
$out"
  fi
  rm -rf "$dir"
}

# -----------------------------------------------------------------------------------------
# (e) a ground_truth mismatch (points at a missing dir) -> exit non-zero
# -----------------------------------------------------------------------------------------
run_case_e() {
  local dir
  dir="$(mktemp -d)"
  make_clean_fixture "$dir"
  # Rewrite the registry's ground_truth to point at a directory that does not exist.
  cat > "$dir/meta/concept-registry.yaml" <<EOF
- id: test-thinking-doctrine
  canonical_home: rules/13-worker-first-mandate.md:Critical Note
  defining_marker: '^##\s+Critical Note\$'
  forbidden_pattern: 'thinking is NOT inherited \(retired\)'

- id: test-cwd-fact
  canonical_home: CLAUDE.md
  ground_truth: 'test -d $dir/this-directory-does-not-exist'
EOF
  local out
  out="$(python3 "$LINT" --root "$dir" 2>&1)"
  local rc=$?
  if [ "$rc" -ne 0 ] && echo "$out" | grep -q "\[test-cwd-fact\] ground_truth failed"; then
    pass "(e) ground_truth mismatch fails and names test-cwd-fact"
  else
    fail "(e) expected non-zero exit naming test-cwd-fact ground_truth -- got rc=$rc, output:
$out"
  fi
  rm -rf "$dir"
}

run_case_a
run_case_b
run_case_c
run_case_d
run_case_e

echo
echo "test-ssot-lint.sh: $PASS_COUNT passed, $FAIL_COUNT failed"
if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
exit 0
