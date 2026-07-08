---
name: test-scaffold
description: "Use when generating pytest boilerplate for untested Python modules."
category: meta
user-invocable: true
argument-hint: "<project-path> [module-path]"
allowed-tools: Bash, Read, Write, Glob, Grep
---

# Test Scaffold Generator

Generate pytest test infrastructure for a project or specific module.

## Steps

1. **Detect project**: Use `$ARGUMENTS` to identify the target project path. If a specific module path is given, scope to that module.

2. **Survey existing tests**:
   - Check for `tests/`, `test/`, or `*_test.py` files
   - Check for `conftest.py`, `pytest.ini`, `pyproject.toml [tool.pytest]`, `setup.cfg [tool:pytest]`
   - Check for existing test conventions (naming, fixtures, markers)

3. **Detect project conventions**:
   - Import style (relative vs absolute)
   - Fixture patterns (conftest.py vs inline)
   - Marker usage (slow, integration, etc.)
   - Monkeypatch vs mock.patch preference

4. **Generate infrastructure** (only if missing):
   - `tests/__init__.py` (empty)
   - `tests/conftest.py` with common fixtures (tmp paths, env vars)
   - `pytest.ini` or `pyproject.toml` section with sensible defaults
   - Do NOT overwrite any existing test files

5. **Generate test stubs** for the target module:
   - One test file per source module (`test_<module>.py`)
   - Import the module under test
   - Create test class or functions matching the module's public API
   - Include `# TODO: implement` markers for each test body
   - Add docstrings describing what each test should verify

6. **Report**:
   - List files created
   - List modules that already had tests (skipped)
   - Suggest next steps (fill in TODOs, run `pytest --co` to verify collection)

## Constraints

- Never overwrite existing test files
- Never modify source code
- Use `monkeypatch` over `@patch` in generated fixtures
- Follow the project's existing naming conventions if tests exist
- For projects with zero tests, default to: `tests/test_<module>.py`, function-based tests, conftest.py fixtures
