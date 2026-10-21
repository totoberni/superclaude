---
name: verify
description: "Use when enforcing evidence-before-claims before reporting completion."
category: workflow
user-invocable: false
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

## Integration with the Completion Report

Before returning ANY `STATUS:` line or completion report:
1. Re-read the task brief's success criteria
2. For each criterion: run the verification command
3. Paste evidence in the report (command + output summary)
4. Only then mark status as DONE

Fresh evidence means run in THIS session — not "it passed earlier" or "the worker said it passed."

## Producers: verify the claim you make, route the rest

A `w-*` producer (w-implementer, w-refactorer, w-debugger, w-doc) is one job (`skills/_shared/dispatch-contract.md` § "One job, done means stop"). Its evidence is ONE smoke check after the final edit (the changed code on a realistic sample, or one targeted test; at most 2 runs). It does not run the full suite, rerun, or sweep to back a claim. Claims the smoke check does not cover are stated as unverified and listed in `NEXT-CHECKS:` (test commands for w-tester, focus for w-reviewer); meta dispatches those in background (W-4). Never write "tests pass" for a suite that was not run.

## Anti-Rationalization

| Excuse | Reality |
|--------|---------|
| "Should work now" | RUN the verification |
| "I'm confident" | Confidence is not evidence |
| "Agent said success" | Verify independently |
| "Partial check is enough" | Partial proves nothing |
| "I'm running low on context" | Verification is the LAST thing you skip |
