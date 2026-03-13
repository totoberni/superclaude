---
name: push
description: "Toggle git push permissions on/off in settings.json."
user-invocable: true
argument-hint: "{true|false}"
allowed-tools: Read, Bash, Edit
---

# Push Toggle

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
