---
name: threat-model
description: "Use when running a STRIDE threat analysis to map and rate attack surface."
category: domain
user-invocable: true
argument-hint: "<target-component-or-project> [--<project>] [--rounds N] [--strict]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Threat Model (STRIDE)

Perform a structured STRIDE threat analysis on a component or project.

**Usage**: `/threat-model <target> [--<project>] [--rounds N] [--strict]`

## Procedure

### 1. Identify Target

Parse `$ARGUMENTS` for the target:
- Component name: `/threat-model auth-middleware`
- Project path: `/threat-model $HOME/projects/workspace/example-enterprise-app`
- File scope: `/threat-model src/api/routes.ts`

If `--<project>` flag present, activate <PROJECT> cross-referencing (see below).

### 2. Map Attack Surface

Read the target codebase and identify:

**Data Flows**:
- External inputs (API endpoints, file uploads, user input, webhooks)
- Data storage (databases, file system, caches, secrets)
- External outputs (API calls, emails, logs, file exports)
- Inter-service communication (queues, RPCs, shared state)

**Trust Boundaries**:
- Client ↔ Server
- Server ↔ Database
- Service ↔ Service
- Internal ↔ External network
- User roles / privilege levels

**External Interfaces**:
- Third-party APIs and SDKs
- Authentication providers (OAuth, SAML, API keys)
- Cloud services (Supabase, AWS, GCP)

**Dependencies**:
- npm/pip packages with known CVE patterns
- Framework versions
- Runtime environment assumptions

### 3. Analyze STRIDE Categories

For each identified asset/flow, evaluate all six STRIDE categories:

| Category | Question | Examples |
|----------|----------|----------|
| **S**poofing | Can an attacker impersonate a legitimate user or component? | Weak auth, token theft, session fixation |
| **T**ampering | Can data be modified in transit or at rest? | Missing integrity checks, unprotected DB writes, MITM |
| **R**epudiation | Can actions be denied without proof? | Missing audit logs, unsigned transactions |
| **I**nformation Disclosure | Can sensitive data leak? | Verbose errors, log exposure, insecure storage, IDOR |
| **D**enial of Service | Can availability be disrupted? | Unbounded queries, missing rate limits, resource exhaustion |
| **E**levation of Privilege | Can an attacker gain higher access? | Broken authz, RBAC bypass, SQL injection, path traversal |

### 4. Rate Each Threat

Use a 3-tier severity rating:

| Severity | Criteria |
|----------|----------|
| **Critical** | Exploitable without authentication, data breach, full system compromise |
| **High** | Exploitable with low-privilege access, significant data exposure |
| **Medium** | Requires specific conditions, limited impact, defense-in-depth gap |
| **Low** | Theoretical risk, minimal impact, defense already present |

### 5. Output Threat Model

```markdown
## Threat Model: <target>

### Attack Surface

#### Data Flows
- [list with direction arrows]

#### Trust Boundaries
- [list boundaries]

#### External Interfaces
- [list interfaces]

### Threat Analysis

| ID | Category | Threat | Asset | Severity | Mitigation | Status |
|----|----------|--------|-------|----------|------------|--------|
| T-001 | [S] Spoofing | ... | ... | Critical | ... | Open |
| T-002 | [T] Tampering | ... | ... | High | ... | Mitigated |

### Summary
- Critical: N threats
- High: N threats
- Medium: N threats
- Low: N threats

### Recommended Actions (priority order)
1. [most critical first]
```

## <PROJECT> Cross-Reference Mode

When `--<project>` flag is set, additionally:

1. Read `RISK_REGISTER.md` in the <PROJECT> project — map each identified threat to existing risk entries
2. Read `CONTROL_CATALOG.md` — check which threats are already covered by existing controls
3. Add columns to the threat table:

| ... | Risk Register | Control | Gap? |
|-----|---------------|---------|------|
| ... | RISK-012 | CTRL-045 | No |
| ... | (none) | (none) | **Yes** |

4. Flag any threats with no matching risk register entry or control as gaps

## Constraints

- Read-only analysis — never modify security configurations
- All paths absolute
- Tag each finding with its STRIDE category: `[S]`, `[T]`, `[R]`, `[I]`, `[D]`, `[E]`
- For multi-service projects, analyze each service boundary separately

## Loop integration (converge)

The procedure above is a single-pass STRIDE analysis with ZERO self-verification: one analyst, one draft, no adversary to probe what was missed. `--rounds N` (or `--strict`) turns it into a `/converge` binding (B1, goal-sealed convergence) that iterates the threat model through produce-then-red-team rounds until a FRESH auditor seals it. Read `/converge` first; this section states only the threat-model deltas. The single-pass analysis above is unchanged and stays the default; the loop is strictly additive and closes the self-verification gap.

Loop orchestration (dispatching the producer, invoking `/review-dispatch`, printing the `/goal` block, spawning the fresh seal auditor) runs in the CONDUCTOR's context (meta or orch, which holds Agent and Skill); threat-model's own single invocation is the STRIDE analysis pass, so its `allowed-tools` cover only that (Read, Write, Edit, Bash, Glob, Grep), not Agent or Skill. The conductor owns the loop, quotes verdicts, and maintains the ledger; threat-model never drives the loop or seals itself.

**Authority**: the single-pass analysis is usable by anyone. The loop dispatches a producer and a red-team reviewer, so it is meta + orch only; a `w-*` worker cannot spawn children, and invoking `--rounds` from a worker is a no-op error.

**Loop body (per round)** fills converge's five steps with threat-model content:

1. **PRODUCE / REVISE**: a producer runs the 5-step STRIDE procedure above end to end (round 1) or revises the threat model (attack surface, rated threats, and their mitigations) against the punch list (later rounds), delegated per `dispatch-contract.md`. Every asset and flow must be evaluated against all six STRIDE categories, every open threat rated and given a mitigation; a model that skips a category, leaves a flow unrated, or leaves a critical unmitigated is not round-complete. The producer returns `STATUS: DONE`; it never self-certifies its own coverage.
2. **PERSIST**: the producer writes the threat model to disk; the conductor appends a ledger entry (round, delta, open-findings count) before the red-team runs.
3. **REVIEW (red-team)**: resolved via `/review-dispatch` on the `methodology` artefact class, which selects `w-hostile-reviewer`. Its adversarial hostile-review gauntlet is a reasonable red-team auditor for a threat model: it maps cleanly onto hunting the attack surface the producer missed. It receives the threat model + diff + rubric ONLY (reviewer isolation), re-examines the CURRENT model with fresh evidence THIS round (no pre-approval), and probes for unmapped attack surface, absent STRIDE categories, under-rated severities, and unmitigated criticals. It emits a `VERDICT` line each round.
4. **REPORT**: the conductor quotes the reviewer's token line verbatim into the ledger, `VERDICT` mid-loop and `SEAL` on the sealing round. Only the reviewer authors tokens; the conductor relays them.
5. **TRIAGE**: accept or contest each finding with evidence (file:line, the STRIDE category the surface falls under, or a concrete exploit path); accepted findings become the next round's punch list, contested ones are logged with a rebuttal.

**Termination (dual condition plus saturation).** The loop ends only when a FRESH `w-hostile-reviewer` returns a clean `SEAL: ACCEPTED` (see the goal block) in the same round as the producer's separate `STATUS: DONE`, AND that red-team round surfaced zero NEW threats or attack-surface gaps (coverage saturation: a cold adversary finds nothing left to add). The SEAL is always a FRESH holistic auditor examining the complete model, never a round reviewer; any change to the model after a SEAL voids it and forces a fresh SEAL (doctrine delta 7, no pre-approval). If total findings do not fall across 2 consecutive rounds, the loop has stalled or is oscillating: ESCALATE rather than burning further rounds.

## Emitted /goal block

Setup ENDS by printing a ready-to-paste `/goal` block, then STOPS; threat-model never arms `/goal` or `/loop` itself (DEC-R2: the external judge stays independent). The block specialises the canonical shape (`_shared/verdict-schema.md`, Canonical emitted /goal block) for a red-team convergence, with the coverage-saturation condition in slot 3:

```
/goal Accept only when ALL hold: (1) the transcript contains a line beginning "SEAL: ACCEPTED" that the conductor states is quoted verbatim from a FRESH w-hostile-reviewer return (red-team pass, methodology class), is the MOST RECENT such line, and post-dates the last change to the threat model, reporting blocking=0 major=0 minor=0 (nits=0 at gate/strict); (2) the producer has separately stated completion (STATUS: DONE); (3) the most recent red-team round surfaced zero NEW threats or attack-surface gaps (coverage saturation). If review rounds exceed <N> (from --rounds, else 4), or total findings do not decrease across 2 consecutive rounds, declare ESCALATE and stop.
```

Paste this to arm the engine; threat-model does not self-arm. The most-recent-and-post-dates clause is load-bearing: a `SEAL: ACCEPTED` recorded before the last edit to the model is stale evidence and never fires the goal (`verdict-schema.md`, No pre-approval). `--strict` tightens clause 1 to require `nits=0` and two consecutive clean SEALs from fresh auditors (submission-grade; `verdict-schema.md` Bar levels); the default bar requires only `blocking=0 major=0`.

## Cross-References

- Convergence engine (binding B1, round order, the 8 loop rules, ledger, caps, DEC-R2): `~/.claude/skills/converge/SKILL.md`
- Reviewer resolution (`methodology` class to the red-team auditor): `~/.claude/skills/review-dispatch/SKILL.md`
- Token protocol, severity map, canonical /goal block, bar levels: `~/.claude/skills/_shared/verdict-schema.md`
- Per-round red-team gauntlet (run by `w-hostile-reviewer`): `~/.claude/skills/hostile-review/SKILL.md`
