---
name: review
description: "Use when invoking w-reviewer on staged changes, general/infra/security"
category: workflow
user-invocable: true
argument-hint: "[mode] [file-or-commit-range]"
context: fork
agent: w-reviewer
---

Review the following changes for code quality, security, and best practices:

Target: $ARGUMENTS

If no target specified:
1. Run `git diff --cached` for staged changes
2. If nothing staged, run `git diff` for unstaged changes
3. If no changes, run `git log -1 --format=%H` and review the last commit

Mode selection (first argument, optional):
- `/review` — auto-detect mode from file paths in the diff
- `/review security` — force security mode (STRIDE checklists)
- `/review infra` — force infra mode (infra-security checklist)
- `/review security src/auth/` — security mode on specific path

## Output Format

Line 1 of the return is the machine-readable token, on every run, so the caller can read the verdict even when the tail is truncated (verdict-first, `verdict-schema.md`):

```
VERDICT: REWORK|CLEAN blocking=N major=N minor=N
```

The human-readable findings follow beneath it, organised by priority (Blocker > High > Medium > Nit), each with a specific file:line reference. The counts map onto the verdict-schema severity row:

| review priority | token field |
|---|---|
| Blocker (or any anti-hacking hit) | `blocking` |
| High | `major` |
| Medium, Nit | `minor` |

`VERDICT: CLEAN` requires `blocking=0 major=0` (default bar); anything above emits `VERDICT: REWORK`.

## Single-pass review

A review is one dispatch and one verdict. The review is isolated (artefact + diff + rubric only; never the producer's reasoning or a prior clean verdict) and examines the CURRENT state with fresh evidence gathered in this review (no pre-approval, `verdict-schema.md`). The caller reads the `VERDICT:` line, addresses the findings, and stops; a re-review happens only if the owner or meta judges it necessary. Only review authors the token, never the caller or the producer under review; a caller that relays the result quotes the VERDICT line verbatim.

**Anti-hacking sweep.** A Blocker fires automatically, in any scope, for the patterns `verdict-schema.md` marks as an automatic `blocking` finding: test special-casing, weakened assertions, harness escapes (forced exit-0), skipped or deleted coverage, diagnostic theatre in infra tests. Any hit forces `VERDICT: REWORK` with `blocking>=1` regardless of the rest of the diff, and it runs on every review without being asked.

## Cross-References

- Token protocol and severity map (Blocker/High/Medium to blocking/major/minor): `~/.claude/skills/_shared/verdict-schema.md`
