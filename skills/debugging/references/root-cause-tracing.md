# Root Cause Tracing

Adapted from obra/superpowers. Trace bugs backward through the call chain to find the original trigger.

## When to Use

- Error happens deep in execution (not at entry point)
- Stack trace shows long call chain
- Unclear where invalid data originated
- Need to find which test/code triggers the problem

## The Tracing Process

### 1. Observe the Symptom

Note the error message, file, line number.

### 2. Find Immediate Cause

What code directly causes this error? Read the function at the error line.

### 3. Ask: What Called This?

Trace one level up the call stack. What value was passed? Where did it come from?

### 4. Keep Tracing Up

Repeat step 3 until you find where the bad value originated. Common sources:
- Test setup returning empty/default values
- Config loaded before initialization
- Import-time evaluation of lazy values
- Shared mutable state from prior test

### 5. Fix at Source

Fix where the bad value ORIGINATES, not where the error APPEARS.

## Adding Stack Traces

When you can't trace manually, add instrumentation:

```python
# Python
import traceback
print(f"DEBUG: value={value}, caller={traceback.format_stack()[-2]}")
```

```typescript
// TypeScript
const stack = new Error().stack;
console.error('DEBUG:', { value, cwd: process.cwd(), stack });
```

Use `console.error()` in tests (loggers may be suppressed).

## Defense in Depth

After fixing at the source, add validation at intermediate layers:
1. Validate inputs at entry points (fail fast)
2. Add assertions at component boundaries
3. Guard dangerous operations (file I/O, git, network)

## Key Principle

NEVER fix just where the error appears. Trace back to find the original trigger.
