---
name: git
description: "Use when the owner toggles agent git commit/push permission"
category: workflow
user-invocable: true
argument-hint: "{true|false}"
allowed-tools: Bash
---

# Git Policy Toggle

Enable or disable ALL agent git commit and push for every agent, by writing the
policy state file that the `26-git-policy` guard reads.

**Argument**: $ARGUMENTS (`true` = commit and push allowed, `false` = both blocked)

## Unattended-context gate

This skill performs a mutating, security-relevant operation (it lifts or lowers a
mechanical block on git history). It is model-invocable, but an agent must NOT run
it to unblock itself. If it is invoked WITHOUT an explicit human instruction to
perform this exact action in the CURRENT session, print the proposed change (the
word it would write and to which file) and STOP; do not execute. Proceed only when
a human has explicitly requested this action this session.

## What the policy does

The SOT is the guard, not this skill. `~/.claude/hooks/guards/26-git-policy.sh`
reads `~/.claude/config/git-policy` on every Bash tool call. When the file says
`disabled`, the guard mechanically BLOCKS any commit or push (git commit,
commit-tree, cherry-pick, revert, am, rebase, merge, push, fast-import, and gh
release create / pr merge / refs-writing api), robust to `git -C <dir>`, inline
`-c` config, chained `cd <dir> && git commit`, `bash -c "..."` wrappers, and
env-prefixed forms. Read-only git (status, log, diff, add) is unaffected.

When blocked, an agent must ask the owner to run `/git true`; the owner manages
git manually (rules/00 Git Discipline; the owner reviews changes before they enter
the commit log). An absent policy file is treated as `enabled`, so the block is
strictly opt-in and current workflows are never broken.

This skill is the sanctioned writer of the policy file. Nothing else should write
it.

## Procedure

### `/git true` (enable commit and push)

```bash
mkdir -p ~/.claude/config && \
printf 'enabled\n' > ~/.claude/config/git-policy && \
echo "git policy is now: $(cat ~/.claude/config/git-policy)"
```

### `/git false` (disable commit and push)

```bash
mkdir -p ~/.claude/config && \
printf 'disabled\n' > ~/.claude/config/git-policy && \
echo "git policy is now: $(cat ~/.claude/config/git-policy)"
```

### Confirm

Report: "Agent git commit and push are now **enabled/disabled**." When disabling,
remind that agents will ask the owner to commit and push from now on.
