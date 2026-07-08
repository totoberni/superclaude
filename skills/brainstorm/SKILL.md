---
name: brainstorm
description: "Design-before-implementation gate for features and architecture."
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

## Loop integration (converge)

`brainstorm` is a design-alignment skill whose convergence criterion is HUMAN APPROVAL, not an automated seal. A `/converge` SEAL must NEVER replace the user's explicit go-ahead at step 5: the HARD-GATE above is unconditional and outranks any converge binding.

The step-5 revise-and-re-present cycle may optionally route each proposal revision through one adversarial `w-reviewer` pre-screen, to sharpen the options before the user sees them. That pre-screen is advisory only and never gates anything; the terminal gate stays the user's explicit approval, full stop. `brainstorm` never emits or accepts a SEAL in place of human approval, and no VERDICT line from the optional pre-screen substitutes for that approval either.

Loop orchestration (dispatching the optional pre-screen reviewer, per `/review-dispatch`) runs in the conductor's context (meta/orch, which holds Agent and Skill), per `converge/SKILL.md`'s Conductor context convention. This skill drives no loop of its own; its own `allowed-tools` cover only the design-presentation flow above.
