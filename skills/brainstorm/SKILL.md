---
name: brainstorm
description: "Use when gating design before implementing a feature or architecture"
category: workflow
user-invocable: true
argument-hint: "[topic or feature description]"
allowed-tools: Read, Glob, Grep, Bash
---

# Brainstorm

Adapted from obra/superpowers. Design before implementation, always.

**Topic**: $ARGUMENTS

<HARD-GATE>
Do NOT write any code, scaffold any project, or take any implementation action until you have presented a design and the user has approved it. This applies regardless of perceived simplicity.
</HARD-GATE>

## Anti-Pattern: "This Is Too Simple To Need A Design"

Every feature goes through this process. A config change, a utility function, a small fix — all of them. "Simple" projects are where unexamined assumptions cause the most wasted work. The design can be short (a few sentences for truly simple things), but you MUST present it and get approval.

## Procedure

### 1. Explore Context

- Check relevant files, docs, recent commits
- Understand the current state before proposing changes
- Identify constraints and dependencies

### 2. Ask Clarifying Questions

- One question at a time (don't overwhelm)
- Prefer multiple-choice when possible
- Focus on: purpose, constraints, success criteria
- Ask until you understand WHAT and WHY

### 3. Propose 2-3 Approaches

For each approach:
- What it does (1-2 sentences)
- Trade-offs (pros/cons)
- Your recommendation and reasoning

Lead with your recommended option.

### 4. Present Design

- Scale detail to complexity (few sentences for simple, paragraphs for complex)
- Cover: architecture, components, data flow, error handling
- Ask after each section: "Does this look right so far?"

### 5. Get Approval

Wait for explicit user approval before ANY implementation. If the user says "looks good" or "go ahead" — that's approval. If they have concerns — address them, revise, re-present.

## Key Principles

- **One question at a time** — don't overwhelm
- **YAGNI ruthlessly** — remove unnecessary features from designs
- **Explore alternatives** — always propose 2-3 approaches
- **Incremental validation** — present design, get approval, then build
- **No premature implementation** — the HARD-GATE is non-negotiable

## Optional reviewer pre-screen

`brainstorm` is a design-alignment skill whose acceptance criterion is HUMAN APPROVAL, not an automated review. No reviewer verdict may EVER replace the user's explicit go-ahead at step 5: the HARD-GATE above is unconditional.

The step-5 revise-and-re-present cycle may optionally route a proposal revision through one adversarial `w-reviewer` pre-screen, to sharpen the options before the user sees them. That pre-screen is a single pass, advisory only, and never gates anything; the terminal gate stays the user's explicit approval, full stop. No `VERDICT:` line from the optional pre-screen substitutes for that approval.

Dispatching the optional pre-screen reviewer happens in the caller's context (meta, or a session that can spawn workers). This skill's own `allowed-tools` cover only the design-presentation flow above.
