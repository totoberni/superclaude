---
paths:
  - "**/*.{ts,tsx,js,jsx}"
  - "**/*.{cpp,hpp,h,cc}"
  - "**/*.py"
  - "**/*.gs"
---

# Coding Standards

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
