---
name: verify
description: "Enforce evidence-before-claims. Loaded by orch before writing RPTs."
category: workflow
user-invocable: false
disable-model-invocation: true
---

# Verification Before Completion

Adapted from obra/superpowers. Evidence before claims, always.

## The Iron Law

```
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

If you haven't run the verification command in THIS session, you cannot claim it passes.

## The Gate Function

BEFORE claiming any status (done, fixed, passing, working):

1. **IDENTIFY**: What command proves this claim?
2. **RUN**: Execute the FULL command (fresh, not cached from earlier)
3. **READ**: Full output, check exit code, count failures
4. **VERIFY**: Does output confirm the claim?
   - If NO: State actual status with evidence
   - If YES: State claim WITH evidence (paste output)
5. **ONLY THEN**: Make the claim

Skip any step = unverified claim.

## Common Verification Requirements

| Claim | Requires | NOT Sufficient |
|-------|----------|----------------|
| "Tests pass" | Test command output: 0 failures | Previous run, "should pass" |
| "Build succeeds" | Build command: exit 0 | Linter passing |
| "Bug fixed" | Original symptom reproduced + now passes | "Code changed, assumed fixed" |
| "Worker completed" | VCS diff shows correct changes | Agent reports "success" |
| "Phase complete" | Line-by-line requirement checklist | "Tests pass" alone |

## Red Flags — STOP and Verify

If you catch yourself using ANY of these words before running verification:
- "should", "probably", "seems to"
- "Great!", "Perfect!", "Done!"
- "I'm confident this works"
- "Just this once, I'll skip the check"

ALL of these mean: STOP. Run the verification command. THEN speak.

## Integration with Orch Protocol

Before writing ANY RPT:
1. Re-read the directive's success criteria
2. For each criterion: run the verification command
3. Paste evidence in the RPT (command + output summary)
4. Only then mark status as DONE

This gate enforces the Pre-Report Compliance Check in `orch.md`. Fresh evidence means run in THIS session — not "it passed earlier" or "the worker said it passed."

## Anti-Rationalization

| Excuse | Reality |
|--------|---------|
| "Should work now" | RUN the verification |
| "I'm confident" | Confidence is not evidence |
| "Agent said success" | Verify independently |
| "Partial check is enough" | Partial proves nothing |
| "I'm running low on context" | Verification is the LAST thing you skip |
