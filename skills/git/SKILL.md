---
name: git
description: "Use when the user explicitly toggles agent git commit and push permission"
category: workflow
user-invocable: true
argument-hint: "{true|false}"
allowed-tools: Bash
---

# Git Policy Toggle

Enable or disable ALL agent git commit and push for every agent, by writing the
policy state file that the `26-git-policy` guard reads.

**Argument**: $ARGUMENTS (`true` = commit and push allowed, `false` = both blocked)

This is the single git commit+push permission toggle. It replaces the old,
separate `/commit` (which used to also stage and execute commits) and `/push`
(which used to toggle a `settings.json` deny rule) skills. `/commit` is now
draft-only (it drafts a conventional commit message and stops; it does not
commit or gate anything, see `skills/commit/SKILL.md`), and `/push` is removed.
The legacy `settings.json` `Bash(git push*)` deny this skill used to coexist
with is retired by the owner-run `~/projects/apply-git-consolidation.sh`
apply-script, so `26-git-policy.sh` is the sole mechanical gate on git commit
and push for every agent.

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

This skill writes only `~/.claude/config/git-policy`; it does not read or write
`settings.json`. `settings.json` is owner-run-only (see rules/12-agent-hierarchy.md).

This guard is a best-effort shell-string heuristic, NOT a shell parser and NOT a
security boundary. Its purpose is to stop an agent's HABITUAL commit/push and
self-unblock attempts, not to defeat a determined adversary deliberately crafting
an evasion. A same-uid agent can still position the verb or the write target via
constructs this heuristic does not model (command substitution, interpreter-driven
git, `eval`, IFS / whitespace-valued variables, and in general arbitrary shell
grammar). The real controls are the owner's manual review of commits and not
granting agents push credentials; filesystem-level ownership would be the only
complete mechanism.

- Tested-and-blocked commit/push vectors: `git -C <dir>` / `-c <k=v>` / `--git-dir`
  / `--work-tree` globals; a chained `cd <dir> && git commit`; the verb after a
  `; && || |` or newline; `bash -c "..."` / `sh -c "..."` / `eval "..."` wrappers
  carrying a literal verb; env-prefix `VAR=val git commit`; an inline
  `-c alias.<name>=<verb>` definition; and a verb obfuscated by quoting, backslash,
  a TAB, a backslash-newline continuation, or a leading redirection (`>f git
  commit`). The companion flag-write check blocks these spellings of the flag path:
  the absolute path, `~/`, `$HOME`/`${HOME}`, `//`, `cd`+relative, a
  `f=<path>; > $f` (and split-var `${b}-policy`) indirection, and a TAB or
  backslash-newline before the target.
- Residual CLASSES it does NOT catch (by design): the verb or the path produced at
  RUNTIME by command substitution (`git $(echo commit)`), `eval`, an
  IFS/whitespace-valued variable (`git${IFS}commit`), or a value pulled from a
  subshell / prior call / environment; an interpreter driving git (python/perl/node);
  a wrapper binary named other than git/gh; and a pre-existing `~/.gitconfig` alias.
  Anything not in the tested list above is a residual, not a promise.

The Write/Edit/MultiEdit route to the flag is covered separately by the
`20-write-acl` guard (those tools name the path as structured data).

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
