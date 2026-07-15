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
release create / pr merge / refs-writing api). Read-only git (status, log, diff,
add) is unaffected.

This guard is BEST-EFFORT, shell-string, defense-in-depth. It is honest about
what that means:

- It DOES catch the common and the simple-deliberate vectors: `git -C <dir>`, a
  chained `cd <dir> && git commit`, `bash -c "..."` / `sh -c "..."` wrappers,
  env-prefixed forms, an inline `-c alias.<name>=<verb>` definition, and a verb
  hidden by quoting or backslashes (`git "commit"`, `\git commit`). On the
  companion flag-write check, it catches the statically spellable path variants
  (`$HOME` / `${HOME}` / `~/`, `//`, `cd`+relative, and a simple
  `f=<path>; ... > $f` variable indirection).
- It CANNOT be complete against a same-uid, Bash-capable agent. A shell string is
  not the program the shell runs. Command substitution (`git $(echo commit)`,
  `echo enabled > $(echo $HOME)/.claude/config/git-policy`), an interpreter driving
  git (python/perl/node), `eval`, and a value pulled from a subshell or the
  environment all produce the verb or the target path at runtime, past any static
  check. Filesystem permissions (a flag dir the agent uid cannot write) would be
  the only true control; this guard is a speed-bump plus a meta-only default-deny.

The real backstop is the owner: manual review of history before it lands, and not
granting push credentials. Do not read the catch-list as a completeness guarantee.

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
