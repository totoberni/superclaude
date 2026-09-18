# Playwright MCP Setup

> Future enhancement for w-design-reviewer. NOT yet installed or configured.

---

## What It Does

[Playwright MCP](https://github.com/microsoft/playwright-mcp) provides browser automation tools as an MCP server. When integrated, w-design-reviewer can:
- Navigate to live preview URLs
- Click, type, and interact with UI elements
- Take screenshots for visual evidence
- Resize viewport for responsive testing
- Read console messages for error checking
- Capture network requests

## Installation (when ready)

Node comes from `fnm` at user scope on this machine; `npm`/`npx` resolve from PATH in a shell where `fnm` is initialised. On Windows an MCP `command` of `npx` may need to be `npx.cmd` (or `cmd /c npx`).

```
npm install -g @anthropic-ai/claude-code-mcp-server-playwright
```

Or use the Playwright MCP server directly:
```
npx @anthropic-ai/claude-code-mcp-server-playwright
```

## MCP Server Config

When ready to enable, add to `~/.claude/settings.json` under a new `mcpServers` key:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@anthropic-ai/claude-code-mcp-server-playwright"],
      "env": {}
    }
  }
}
```

**Important**: Do NOT add this config until explicitly instructed by the user. `settings.json` is owner-managed (see `docs/win11-migration/apply-settings.ps1`); the owner makes the edit, keeping a backup and validating the JSON afterwards.

## Agent Updates (when ready)

Update `~/.claude/agents/w-design-reviewer.md`:

1. Add Playwright MCP tools to the `tools:` frontmatter field:
   ```yaml
   tools: Read, Grep, Glob, Bash, mcp__playwright__browser_navigate, mcp__playwright__browser_click, mcp__playwright__browser_type, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_resize, mcp__playwright__browser_snapshot, mcp__playwright__browser_console_messages, mcp__playwright__browser_select_option, mcp__playwright__browser_press_key
   ```

2. Update the "Operating Mode" section to indicate Playwright is available

3. Phases 1-2 can then use live browser testing instead of static code analysis

## Verification

After setup, verify with:
```
# Check MCP server is reachable
npx @anthropic-ai/claude-code-mcp-server-playwright --help

# In Claude Code, verify tools are available
# The agent should see mcp__playwright__* tools in its tool list
```

## Status

- [ ] npm package installed
- [ ] MCP server config added to settings.json
- [ ] w-design-reviewer.md tools updated
- [ ] Tested with a live frontend review
