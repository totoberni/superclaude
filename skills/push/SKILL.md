---
name: push
description: "Use when the user explicitly asks to toggle agent git-push permission"
category: workflow
user-invocable: true
argument-hint: "{true|false}"
allowed-tools: Read, Bash, Edit
---

# Push Toggle

## Unattended-context gate

This skill performs a mutating or irreversible operation and is now model-invocable. If it is invoked WITHOUT an explicit human instruction to perform this exact action in the CURRENT session, print the proposed mutation (the exact command or change it would make) and STOP; do not execute. Proceed only when a human has explicitly requested this action this session.

Enable or disable `git push` for all agents by modifying `~/.claude/settings.json` deny rules.

**Argument**: $ARGUMENTS (`true` = push allowed, `false` = push blocked)

## Procedure

### `/push true` (enable push)

Remove push deny rules from settings.json:

```bash
cp ~/.claude/settings.json ~/.claude/settings.json.bak && \
jq '.permissions.deny |= [.[] | select(startswith("Bash(git push") | not)]' \
  ~/.claude/settings.json > /tmp/settings-push.tmp && \
mv /tmp/settings-push.tmp ~/.claude/settings.json && \
jq . ~/.claude/settings.json > /dev/null && \
echo "Push ENABLED" || \
(cp ~/.claude/settings.json.bak ~/.claude/settings.json && echo "FAILED — restored backup")
```

### `/push false` (disable push)

Add push deny rules to settings.json (only if not already present):

```bash
cp ~/.claude/settings.json ~/.claude/settings.json.bak && \
jq '.permissions.deny |= (. + ["Bash(git push *)", "Bash(git push)"] | unique)' \
  ~/.claude/settings.json > /tmp/settings-push.tmp && \
mv /tmp/settings-push.tmp ~/.claude/settings.json && \
jq . ~/.claude/settings.json > /dev/null && \
echo "Push DISABLED" || \
(cp ~/.claude/settings.json.bak ~/.claude/settings.json && echo "FAILED — restored backup")
```

### Confirm

After either toggle, show the current deny list:

```bash
jq '.permissions.deny' ~/.claude/settings.json
```

Report: "Push is now **enabled/disabled**."
