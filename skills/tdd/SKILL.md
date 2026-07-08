---
name: tdd
description: "Use when running a RED-GREEN-REFACTOR cycle for features and bugfixes."
category: workflow
user-invocable: true
argument-hint: "[task-description]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Test-Driven Development

Adapted from obra/superpowers. Write the test first. Watch it fail. Write minimal code to pass.

## The Iron Law

```
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

Wrote code before the test? Delete it. Start over. No exceptions.

## RED-GREEN-REFACTOR Cycle

### RED — Write Failing Test

1. Write ONE minimal test showing what should happen
2. Clear test name describing the behavior (not "test1")
3. Test real code, not mocks (unless unavoidable)

```bash
# Run the test — it MUST fail
pytest <test_file> -x -v    # Python
npm test <test_file>         # TypeScript/JS
```

**Verify the failure**:
- Test fails (not errors from syntax/import)
- Failure message matches expected behavior gap
- Fails because feature is missing, not because of typos

Test passes immediately? You're testing existing behavior. Fix the test.

### GREEN — Minimal Code

1. Write the SIMPLEST code that makes the test pass
2. No extra features, no "while I'm here" improvements
3. No over-engineering (YAGNI)

```bash
# Run the test — it MUST pass now
pytest <test_file> -x -v    # Timeout: use `timeout 600000` for Bash tool
```

**Verify**: test passes, other tests still pass, no warnings.

Test fails? Fix the CODE, not the test.

### REFACTOR — Clean Up

Only after green:
- Remove duplication
- Improve names
- Extract helpers

Keep tests green throughout. Don't add behavior.

### Repeat

Next failing test for the next piece of behavior.

## Commit Convention

- `test:` prefix for test additions (RED phase)
- `feat:` or `fix:` prefix for implementation (GREEN phase)
- `refactor:` prefix for cleanup (REFACTOR phase)

## Bug Fix with TDD

1. Write a failing test that reproduces the bug
2. Verify it fails for the RIGHT reason (the bug, not a typo)
3. Fix the bug (minimal change)
4. Verify the test passes
5. Run the full suite — no regressions

## Anti-Rationalization

Thinking "skip TDD just this once"? Stop. That's rationalization.

| Excuse | Reality |
|--------|---------|
| "Too simple to test" | Simple code breaks. Test takes 30 seconds. |
| "I'll test after" | Tests passing immediately prove nothing. |
| "Need to explore first" | Fine. Throw away exploration, start with TDD. |
| "TDD will slow me down" | TDD is faster than debugging. |
| "Already manually tested" | Manual is ad-hoc. No record, can't re-run. |

## When Stuck

| Problem | Solution |
|---------|----------|
| Don't know how to test | Write the wished-for API. Assertion first. |
| Test too complicated | Design too complicated. Simplify interface. |
| Must mock everything | Code too coupled. Use dependency injection. |
| Test setup huge | Extract helpers. Still complex? Simplify design. |

## Verification Checklist

Before marking work complete:
- [ ] Every new function has a test
- [ ] Watched each test fail before implementing
- [ ] Wrote minimal code to pass each test
- [ ] All tests pass (run full suite, not just new tests)
- [ ] No warnings or errors in output

## Loop integration (converge)

The RED-GREEN core is already self-gating: the test suite is a deterministic pass/fail gate, and no LLM review loop is needed to decide whether a test passes. `/converge` has nothing to add there.

REFACTOR is the optional converge touchpoint. Once GREEN holds, the conductor may run `/converge` (or a single `w-reviewer` pass) over the refactored code to iterate code quality (naming, duplication, structure) to clean, while the correctness gate stays the test suite throughout: REFACTOR review only proceeds on a green suite, and every round must keep it green.

Loop orchestration (dispatching the reviewer, invoking `/review-dispatch`, printing the `/goal` block) runs in the conductor's context (meta/orch, which holds Agent and Skill), per `converge/SKILL.md`'s Conductor context convention. This skill drives no loop of its own; its own `allowed-tools` cover only the single RED-GREEN-REFACTOR cycle above.
