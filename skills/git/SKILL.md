---
name: git
description: "Use when the user explicitly toggles agent git commit and push permission"
category: workflow
user-invocable: true
argument-hint: "{true|false}"
allowed-tools: Read, Write
---

# Git Policy Toggle

Enable or disable ALL agent git commit and push for every agent of the ACTIVE
profile, by writing the policy state file that the git-policy guard reads.

**Argument**: $ARGUMENTS (`true` = commit and push allowed, `false` = both blocked)

This is the single git commit+push permission toggle. It replaces the old,
separate `/commit` (which used to also stage and execute commits) and `/push`
(which used to toggle a `settings.json` deny rule) skills. `/commit` is now
draft-only (it drafts a conventional commit message and stops; it does not
commit or gate anything, see `skills/commit/SKILL.md`), and `/push` is removed.
The legacy `settings.json` `Bash(git push*)` deny this skill used to coexist
with is retired, so the git-policy guard is the sole mechanical gate on git commit
and push for every agent.

## Unattended-context gate

This skill performs a mutating, security-relevant operation (it lifts or lowers a
mechanical block on git history). It is model-invocable, but an agent must NOT run
it to unblock itself. If it is invoked WITHOUT an explicit human instruction to
perform this exact action in the CURRENT session, print the proposed change (the
word it would write and to which file) and STOP; do not execute. Proceed only when
a human has explicitly requested this action this session.

## Which flag file

Each profile has its own flag, `<data home>/config/git-policy`:

| `SC_PROFILE` seen by the session | Profile | Flag file |
|---|---|---|
| empty or unset | superclaude | `~/.claude/config/git-policy` |
| `deepseek` | DeepSeek | `~/.claude-deepseek/config/git-policy` |

Find the value with a read-only probe: `echo "$SC_PROFILE"` (Bash) or
`$env:SC_PROFILE` (PowerShell). A DeepSeek session gets `deepseek` from its
launcher and settings. Any other value: report it and stop. Never assign
`SC_PROFILE` (the profile firewall denies it). Write only the active profile's
flag; the profile firewall denies writing the other profile's flag.

## What the policy does

The SOT is the guard, not this skill. The git-policy guard
(`hooks/sc_hooks/handlers/git_policy.py`) runs on every Bash and PowerShell tool
call and reads the active profile's flag. Accepted words, case-insensitive:
`enabled`, `true`, `on`, `1`, `yes` and `disabled`, `false`, `off`, `0`, `no`.

- superclaude, fail-open: the block applies only when the file exists, is
  readable, and says a disabling word. An absent file or any other content counts
  as `enabled`. `SUPERCLAUDE_GIT_POLICY_FILE` overrides the path (tests).
- DeepSeek, fail-closed: git is unblocked only when the file exists, is readable,
  and says an enabling word. An absent or unreadable file or any other content
  counts as `disabled`; `SUPERCLAUDE_GIT_POLICY_FILE` is ignored. DeepSeek init
  writes `false`, so DeepSeek git stays off until the owner runs `/git true` in a
  DeepSeek session.

While `disabled`, the guard BLOCKS, for every role (plain, meta, worker):

- `git commit`, `commit-tree`, `push`, `rebase`, `merge`, `cherry-pick`, `revert`,
  `am`, `fast-import`;
- `git reset --hard` (the flag in any position);
- `git tag` when it creates or deletes a tag;
- `gh pr merge` and `gh release create`.

These forms PASS while disabled:

- read-only and staging git: `status`, `log`, `diff`, `show`, `add`, `fetch`,
  `pull`, `stash`, `checkout`, `branch`, `reset` without `--hard`;
- `git tag` with no arguments or with a listing option (`-l`, `--list`, `-n`,
  `-v`, `--verify`, `--contains`, `--no-contains`, `--points-at`, `--merged`,
  `--no-merged`, `--sort`, `--format`, `--column`);
- `--abort` and `--quit` on `rebase`, `merge`, `cherry-pick`, `revert`, `am`, so
  an agent can always back out of an operation in progress;
- other `gh` subcommands (`pr view`, `pr list`, `pr create`, `release list`);
- a verb that only appears inside a quoted string (`echo 'git commit'`).

This skill writes only the active profile's flag file; it does not read or write
`settings.json`. `settings.json` is owner-run-only (see rules/12-agent-hierarchy.md).

How the guard reads a command: it splits the string on `;`, `&&`, `||`, `|`, `&`
and newlines (respecting quotes), rejoins backslash and backtick line
continuations, skips harmless prefixes (`VAR=val`, `env`, `sudo`, `command`,
`exec`, the PowerShell call operator `&`), accepts `git`, `git.exe` or a quoted
full path to it, steps over git global options (`-C <dir>`, `-c <k=v>`,
`--git-dir=...`), and looks up to two levels into the quoted argument of a
wrapper (`bash -c`, `sh -c`, `pwsh -Command`, `powershell`, `cmd`, `eval`, `iex`).

Flag-file write protection: independently of the flag state, a worker's shell
command that targets the active profile's policy file is denied. This covers a redirection
(`>`, `>>`, `>|`) and the common writer commands in both shells (`tee`, `cp`,
`mv`, `rm`, `sed`, `dd of=`, `Set-Content`, `Add-Content`, `Out-File`,
`Copy-Item`, `Move-Item`, `Remove-Item`, `New-Item` and their aliases), with the
path spelled absolutely, through `~`, `$HOME`, `$env:USERPROFILE`, or relative to a
preceding `cd` / `Set-Location`. Reading the flag is allowed. Meta and plain
sessions are not restricted by this check; for them the unattended-context gate
above is the control.

This guard is a best-effort speed bump built on readable heuristics, NOT a shell
parser and NOT a security boundary. Its purpose is to stop an agent's HABITUAL
commit/push and self-unblock attempts, not to defeat a deliberately crafted
evasion. Known residual gaps, by design:

- variable indirection and anything produced at runtime: a verb or path held in
  a variable, command substitution (`$(...)`, backticks), a quoted verb
  (`git "commit"`);
- aliases: an inline `-c alias.<name>=<verb>` definition or a pre-existing
  `~/.gitconfig` alias, since the guard sees the alias name, not the verb;
- `gh api` (refs-writing REST calls are not inspected) and any other `gh`
  subcommand beyond the two listed above;
- an interpreter or script driving git (python, node, a `.ps1` or `.sh` file),
  and a wrapper binary named other than `git` / `gh`.

The real controls are the owner's manual review of commits and not granting
agents push credentials.

The Write/Edit/MultiEdit route to the flag is covered separately by the
write-ACL guard (those tools name the path as structured data).

When blocked, an agent must ask the owner to run `/git true`; the owner manages
git manually (rules/00 Git Discipline; the owner reviews changes before they enter
the commit log). Under superclaude an absent policy file is treated as `enabled`,
so the block is strictly opt-in; under DeepSeek it is treated as `disabled`.

This skill is the sanctioned writer of the policy file. Nothing else should write
it.

## Procedure

### `/git true` (enable commit and push)

Determine the flag file (see Which flag file), then Write it with the Write tool
(parents are created automatically). The file content is the single word `enabled` followed by a
newline. Then Read the file back and report "git policy is now: enabled".

### `/git false` (disable commit and push)

Determine the flag file (see Which flag file), then Write it with the Write tool.
The file content is the
single word `disabled` followed by a newline. Then Read the file back and report
"git policy is now: disabled".

### Confirm

Report: "Agent git commit and push are now **enabled/disabled** for the
<superclaude|DeepSeek> profile." When disabling,
remind that agents will ask the owner to commit and push from now on.
