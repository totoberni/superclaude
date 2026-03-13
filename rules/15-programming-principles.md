# Programming Principles

These principles govern ALL code written or reviewed by agents. Violations require explicit justification and the user's approval.

## 1. DRY — Don't Repeat Yourself
- Every piece of knowledge has a single, authoritative representation
- Files, functions, and variables have SEPARATE CONCERNS (roles, functionalities, use cases)
- Comments REFERENCE other files/functions rather than duplicating their documentation
- Extract shared logic into well-named functions when used 3+ times
- Exception: duplication is acceptable when decoupling is more important (requires the user's approval)

## 2. Self-Documenting Code
- Code is the primary documentation of HOW things work
- Clear naming is the first line of defense: function names describe actions, variables describe content
- Comments explain WHY, never WHAT — if a comment describes what code does, refactor the code instead
- Acceptable comments: intent behind non-obvious decisions, performance trade-offs, workarounds with ticket refs, regex explanations, algorithm citations
- Never leave outdated comments — an outdated comment is worse than no comment
- Function signatures should be self-explanatory; add a one-liner only if the name isn't sufficient

## 3. READMEs Are Poetry
- READMEs are compact, information-dense, and structured (ToC + sections)
- READMEs cover ONLY: Setup, Usage, Configuration, Contributing
- READMEs do NOT explain how files work — that belongs in the code itself (see #2)
- Every word earns its place; no essays, no filler
- Use tables for reference data, code blocks for commands, links for external docs

## 4. Write Simple, Extend Later
- Start with a well-structured, functional prototype before optimizing
- Prefer flat over nested, explicit over clever, boring over novel
- Add complexity only when requirements demand it, not in anticipation
- YAGNI: You Aren't Gonna Need It — don't build for hypothetical futures
- KISS: the simplest solution that correctly solves the problem wins

## 5. The Programmer as the Source of Bugs
- The primary cause of bugs is the programmer. Write with this awareness
- Before committing any change, ask:
  - What will this break? (dependency analysis)
  - What assumptions am I making? (precondition check)
  - What happens at the boundaries? (edge cases: null, empty, overflow, concurrency)
  - Will future-me understand this in 6 months? (readability check)
  - What error paths exist? (failure mode analysis)
- Validate inputs at system boundaries, trust internal code
- Fail fast: surface errors immediately rather than propagating corrupt state
- Assertions document and enforce assumptions in critical paths

## 6. Write for Other Humans
- Code will be maintained by others (or future-you who has forgotten context)
- Document obscure or non-obvious approaches with a brief WHY comment
- Maintain consistent patterns within a codebase — don't introduce novel patterns without reason
- Naming conventions are sacred: follow the project's existing patterns
- Make code reusable where natural, but don't force abstraction

## 7. Separation of Concerns
- Each module/file/function has ONE clear responsibility
- Changes to one concern should not require changes to unrelated concerns
- Interface boundaries should be narrow and well-defined
- High cohesion within modules, low coupling between modules
- When a function does too much, split it — the name should describe ALL it does

## 8. Defensive Design
- Design by Contract: functions have clear preconditions, postconditions, and invariants
- Check return values of fallible operations
- Handle errors explicitly — no silent swallowing
- Prefer immutable data; mutate only when necessary
- Minimize variable scope — declare at the narrowest scope possible
- Avoid shared mutable state

## 9. Complexity Budget
- Functions: max ~40 lines of logic (if it doesn't fit on a screen, split it)
- Parameters: max 3-4 per function; use an options/config object beyond that
- Nesting: max 3 levels deep; use early returns / guard clauses to flatten
- Cyclomatic complexity >10 = must refactor
- Cognitive complexity >15 = must refactor
- If you need a comment to explain control flow, the flow is too complex

## 10. Code Review Mindset
- Technical facts and data overrule opinions (Google engineering standard)
- Every change should improve overall code health — never approve degradation
- Look for: design fit, functionality, complexity, tests, naming, comments
- Over-engineering is a defect: solving hypothetical future problems adds real current complexity
- The best code review catches bugs the AUTHOR injected without noticing

## Override Protocol
To bypass any principle above, the agent must:
1. State which principle is being bypassed
2. Explain WHY the bypass is necessary
3. Get explicit approval from the user before proceeding
