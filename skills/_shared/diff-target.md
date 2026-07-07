# _shared/diff-target.md: canonical diff-target resolution ladder (SOT)

Consumed by: /commit, /review, /design-review.

## Step A -- explicit argument (skip to Step B if no argument, or the argument is a mode keyword consumed separately)

Parse the argument by shape, in this order:

1. **Numeric** (e.g. `42`) -> PR number: `gh pr diff <N> [-- <path-filters>]` + `gh pr view <N> --json title,body` for the description.
2. **Branch name** (e.g. `feature/x`) -> diff against the base branch: `git diff main...<branch> [-- <path-filters>]`.
3. **File path** (e.g. `src/x.tsx`) -> review/diff that file directly.
4. **Commit range / other explicit target string** -> use as the literal diff target (e.g. `git diff <range>`).

Path filters (`-- '*.tsx' '*.jsx' ...`) apply only when the consumer scopes to a file-type subset (design-review's frontend extensions); omit otherwise.

## Step B -- no argument resolved: auto-detect from the working tree

1. **Staged**: `git diff --cached` (filtered variant: `--name-only -- <filters>`). Non-empty -> stop here.
2. **Unstaged**: `git diff` (same filter rule). Non-empty -> stop here.
3. **Last commit**: `git log -1 --format=%H`, then diff/review that commit. Only reachable when the consumer has no narrower natural stopping point.

## Per-consumer tier map

| Consumer | Step A tiers used | Step B depth | Notes |
|---|---|---|---|
| /commit | none (always the working tree) | staged -> unstaged only | No tier 3: nothing staged/unstaged means nothing to commit -- report clean, don't fall back to history. |
| /review | file-path / commit-range | staged -> unstaged -> last-commit (full 3-tier) | Full ladder: a review target must always resolve to something, even a clean tree's last commit. |
| /design-review | PR# / branch / file (full Step A) | staged + unstaged, frontend-filtered, checked together (not sequential) | No tier 3: no frontend changes anywhere -> report "No frontend changes detected", ask for an explicit target. |

## Rule

Never invent a further tier (e.g. diffing against `origin/main`) without the consumer explicitly asking -- silently widening the target changes what gets reviewed or committed without the user's knowledge.

## Cross-reference

/review and /design-review verdicts, once the target is resolved, follow [verdict-schema.md](verdict-schema.md)'s VERDICT token.
