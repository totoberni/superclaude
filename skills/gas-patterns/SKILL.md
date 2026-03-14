---
name: gas-patterns
description: "Google Apps Script conventions and gotchas. For w-debugger."
category: domain
user-invocable: false
---

# Google Apps Script Patterns

Conventions, workflows, and gotchas for Google Apps Script development with clasp.

## Code Conventions

- **`var` only** — no `let`/`const` (enterprise convention across all GAS projects)
- **Service objects as singletons** — e.g., `var ss = SpreadsheetApp.getActiveSpreadsheet()`
- **No ES6 modules** — all .gs files share one global scope; no import/export
- **Error handling** — `try/catch` → `LogService.log()` or `console.error()` → return error JSON
- **HTTP responses** — always via `ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON)`
- **Config** — use `PropertiesService.getScriptProperties()`, never hardcode IDs/URLs/tokens
- **muteHttpExceptions: true** — in EVERY `UrlFetchApp.fetch()` call, no exceptions

## Clasp Workflow

| Command | What It Does | When to Use |
|---------|-------------|-------------|
| `clasp push -f` | Force push local .gs files to Apps Script project | After editing source files |
| `clasp deploy -i <deployment-id> -d "message"` | Update an existing deployment in-place | After push, to update /exec endpoint |
| `clasp deployments` | List all deployment IDs and versions | To find the deployment ID |
| `clasp open` | Open the script in the browser IDE | For authorization, debugging, manual runs |

## Critical Gotchas

| Gotcha | Explanation | Fix |
|--------|-------------|-----|
| clasp push does NOT update deployments | `/exec` URL serves the last deployed version, not HEAD | Always run `clasp deploy -i <id>` after `clasp push` |
| Re-authorization for new scopes | Adding a new service (e.g., DriveApp) requires OAuth re-consent | Run the function once in the IDE (Run button) to trigger the auth dialog |
| curl POST to /exec returns 405 | Google redirects /exec with 302; `-X POST` doesn't follow redirect correctly | Use `curl -L -d '' <url>` instead of `curl -X POST <url>` |
| appsscript.json access != deployment access | Scopes in manifest don't auto-propagate to existing deployments | After changing scopes: push → open IDE → Deploy > Manage deployments > update |

## Module Reference Rule

When referring to a GAS function, always specify the source module:
`MODULO_XX_NOME → nomeFunzione()` — never just "execute nomeFunzione()" without the module context.

This prevents ambiguity when multiple .gs files define similar helper names in the shared global scope.
