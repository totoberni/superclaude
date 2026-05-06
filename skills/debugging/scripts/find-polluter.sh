#!/usr/bin/env bash
# Bisection script to find which test creates unwanted files/state
# Adapted from obra/superpowers for superclaude infrastructure
#
# Usage: find-polluter.sh <file_or_dir_to_check> <test_pattern>
# Example: find-polluter.sh '.git' 'tests/**/*.py'
# Example: find-polluter.sh '__pycache__/stale.pyc' 'src/**/*.test.ts'

if [ $# -ne 2 ]; then
  echo "Usage: $0 <file_to_check> <test_pattern>"
  echo "Example: $0 '.git' 'tests/**/*.py'"
  exit 1
fi

POLLUTION_CHECK="$1"
TEST_PATTERN="$2"

echo "Searching for test that creates: $POLLUTION_CHECK"
echo "Test pattern: $TEST_PATTERN"
echo ""

# Get list of test files
TEST_FILES=$(find . -path "$TEST_PATTERN" 2>/dev/null | sort)
TOTAL=$(echo "$TEST_FILES" | wc -l | tr -d ' ')

if [ "$TOTAL" -eq 0 ]; then
  echo "No test files found matching: $TEST_PATTERN"
  exit 1
fi

echo "Found $TOTAL test files"
echo ""

# Detect test runner
if [ -f "pytest.ini" ] || [ -f "pyproject.toml" ] || [ -f "setup.cfg" ]; then
  RUNNER="pytest"
elif [ -f "package.json" ]; then
  RUNNER="npm test"
else
  RUNNER="pytest"
fi

COUNT=0
for TEST_FILE in $TEST_FILES; do
  COUNT=$((COUNT + 1))

  if [ -e "$POLLUTION_CHECK" ]; then
    echo "WARNING: Pollution already exists before test $COUNT/$TOTAL"
    echo "   Skipping: $TEST_FILE"
    continue
  fi

  echo "[$COUNT/$TOTAL] Testing: $TEST_FILE"

  if [ "$RUNNER" = "pytest" ]; then
    python3 -m pytest "$TEST_FILE" -x -q > /dev/null 2>&1 || true
  else
    $RUNNER "$TEST_FILE" > /dev/null 2>&1 || true
  fi

  if [ -e "$POLLUTION_CHECK" ]; then
    echo ""
    echo "FOUND POLLUTER!"
    echo "   Test: $TEST_FILE"
    echo "   Created: $POLLUTION_CHECK"
    echo ""
    echo "Pollution details:"
    ls -la "$POLLUTION_CHECK"
    echo ""
    echo "To investigate:"
    echo "  $RUNNER $TEST_FILE"
    exit 1
  fi
done

echo ""
echo "No polluter found - all tests clean!"
exit 0
