---
paths:
  - "**/*.{ts,tsx,js,jsx}"
  - "**/*.{cpp,hpp,h,cc}"
  - "**/*.py"
  - "**/*.gs"
  - "**/*.{sh,bash}"
---

# Coding Standards

## General Principles (all languages)

These apply before any language-specific rule. They're stressed here because LLMs are prone to **slopification** — accreting patches on top of broken code instead of removing the broken code.

- **Subtract before you add.** When a script that used to work starts failing, your FIRST hypothesis must be "what recent addition introduced the regression?" REVERT that addition, understand the underlying cause, THEN add back only what's strictly necessary. Writing a new patch on top of an existing patch on top of an existing patch is a code smell — the accumulated mass is usually worse than the original bug. A refactor that deletes 20 lines and fixes the bug beats a refactor that adds 20 lines and masks it.
- **Converge on working patterns, don't invent new ones.** When two files/scripts solve similar problems and one works while the other doesn't, make the broken one MATCH the working one. Do not invent a third pattern. Consistency across a codebase is a feature, not a coincidence. If you find yourself writing a fix that differs structurally from the 80% of the codebase that already works, stop and ask why.
- **Single source of truth.** When a value can be computed in two places (e.g., a path constructed in both bash and Python), pick ONE site as canonical. The other site must either consume the canonical value directly or not exist. "Mirroring" a computation for "early failure detection" or "clarity" is how divergence bugs are born.
- **Project files cite project files only.** Local codebase files (`*.sh`, `*.md`, `*.py`, etc. under `~/projects/*`) must NOT reference superclaude-internal artefacts by name — no `~/.claude/...`, no `example-project.md`, no `MEMORY.md`, no agent-memory mistake IDs (`M-9`, `MM-11`, `GM-1`, etc.). Meta-tier memory is private to the agent hierarchy; a teammate cloning the repo or a reviewer reading a paper submission sees dangling references that resolve to nothing. The content of superclaude memory MAY inform what you write locally (paraphrased inline, or as a local file reference like `docs/reprod-notes.md §C4`) — but the meta-structure and its filenames/IDs must stay invisible from inside the project. Conversely, superclaude memory MAY freely reference local paths and content (one-way flow: meta reads local, local does not read meta). Before any write to a project file, grep your draft for the forbidden patterns and strip them.
- **See also:** `15-programming-principles.md` §4 (Write Simple, Extend Later — KISS/YAGNI) and §10 (Code Review Mindset: "over-engineering is a defect").

## TypeScript / JavaScript (*.ts, *.tsx, *.js, *.jsx)
- Strict mode, no `any` types
- Interfaces for contracts, types for unions
- camelCase variables, PascalCase types/components
- ES modules (import/export), not CommonJS

## C++ (*.cpp, *.hpp, *.h)
- C++17 standard
- CPM.cmake for dependencies — pin exact versions
- Components = plain structs, no inheritance for ECS
- Always `<random>`, never `std::rand()`
- `float` for sim values, never `double`

## Python (*.py)
- Type hints on function signatures
- `if __name__ == "__main__":` guard on scripts
- Requirements in requirements.txt, pinned versions

## Google Apps Script (*.gs)
- `var` only — no `let`/`const` (enterprise convention)
- `muteHttpExceptions: true` in every UrlFetchApp.fetch()
- No import/export — all .gs files share one global scope
- Config via Script Properties, never hardcoded
