---
name: code-quality
description: "Use when reviewing code quality against programming principles"
category: meta
user-invocable: false
---

# Code Quality Checklist

This checklist operationalizes the programming principles. Use it systematically when reviewing or refactoring code.

## DRY Analysis
- [ ] Search for duplicated logic: `grep -rn` for similar function bodies, copy-pasted blocks
- [ ] Each function/module has a single, clear responsibility
- [ ] Shared logic extracted when used 3+ times
- [ ] Comments reference other files/functions instead of duplicating their docs
- [ ] No "Oddball Solutions" — similar problems solved the same way across codebase

## Self-Documentation Check
- [ ] Function names describe their action (verb + noun): `calculateTotal`, `fetchUserById`
- [ ] Variable names describe their content: `remainingRetries`, not `r` or `count`
- [ ] Boolean names read as questions: `isValid`, `hasPermission`, `shouldRetry`
- [ ] Constants have semantic names: `MAX_RETRY_COUNT`, not `3`
- [ ] No magic numbers or strings — all literals named
- [ ] Comments explain WHY (intent, trade-offs, workarounds), never WHAT
- [ ] No outdated or misleading comments (worse than no comments)
- [ ] Complex regex or algorithms have explanatory comments with references

## Complexity Budget
- [ ] Functions ≤40 lines of logic
- [ ] Parameters ≤4 per function (use options/config object beyond)
- [ ] Nesting ≤3 levels (use early returns / guard clauses)
- [ ] No function does more than its name says
- [ ] Control flow readable without comments (if not, simplify)
- [ ] Prefer flat over nested, explicit over clever

## Defensive Design
- [ ] Inputs validated at system boundaries (API endpoints, user input, external data)
- [ ] Internal functions trust their callers (no redundant validation)
- [ ] Return values of fallible operations checked
- [ ] Error handling explicit — no empty catch blocks, no silent swallowing
- [ ] Errors fail fast: surface immediately, don't propagate corrupt state
- [ ] Variables declared at narrowest scope
- [ ] Mutable state minimized; prefer immutable/const where possible

## Bug Injection Analysis
For every change, ask:
- [ ] What downstream code depends on this? Will it break?
- [ ] What assumptions does this code make? Are they documented/validated?
- [ ] What happens with: null/undefined, empty string, empty array, zero, negative numbers, MAX_INT?
- [ ] What happens under: concurrent access, network failure, disk full, permission denied?
- [ ] Are there race conditions or timing dependencies?
- [ ] Will future-me understand this in 6 months without context?

## Code Smells to Flag
Priority smells from Fowler's catalog (flag these immediately):
- **Duplicated Code** — "one of the worst smells"
- **Long Method** — function too long to understand at a glance
- **Feature Envy** — method uses more features of another class than its own
- **Shotgun Surgery** — one change requires edits across many files
- **Dead Code** — unreachable code, unused variables, commented-out blocks
- **Speculative Generality** — abstractions for hypothetical futures (YAGNI violation)
- **Magic Numbers** — unexplained literals
- **Primitive Obsession** — using primitives where a domain type would be clearer
- **Long Parameter List** — function takes too many arguments
- **Mutable Data** — shared mutable state without synchronization

## Separation of Concerns
- [ ] Each file has a clear, single purpose (stated in a header comment if non-obvious)
- [ ] Interface boundaries are narrow (minimal public API surface)
- [ ] High cohesion within modules (related things together)
- [ ] Low coupling between modules (changes don't ripple)
- [ ] No circular dependencies between modules/files

## For Other Humans
- [ ] Non-obvious approaches documented with brief WHY comment
- [ ] Consistent patterns within the codebase (don't introduce novel patterns without reason)
- [ ] Naming conventions match project's existing style
- [ ] Code reusable where natural (but no forced abstractions)
- [ ] Error messages actionable (tell the user what to do, not just what failed)

## Sources
These principles are synthesized from:
- the user's programming philosophy (rules/15-programming-principles.md)
- Google Engineering Practices (code review standard)
- Robert C. Martin — Clean Code, SOLID principles
- Martin Fowler — Refactoring, Code Smells catalog
- NASA JPL — Power of Ten rules (safety-critical adaptations)
- Edsger Dijkstra — The Humble Programmer (ACM 1972)
- Bertrand Meyer — Design by Contract
- SonarSource — Cognitive/Cyclomatic complexity metrics
