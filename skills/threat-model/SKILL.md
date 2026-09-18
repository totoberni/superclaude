---
name: threat-model
description: "Use when running a STRIDE threat analysis to map and rate attack surface."
category: domain
user-invocable: true
argument-hint: "<target-component-or-project> [--<project>]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Threat Model (STRIDE)

Perform a structured STRIDE threat analysis on a component or project.

**Usage**: `/threat-model <target> [--<project>]`

## Procedure

### 1. Identify Target

Parse `$ARGUMENTS` for the target:
- Component name: `/threat-model auth-middleware`
- Project path: `/threat-model ~/code/example-enterprise-app`
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

## Red-team pass (single review)

The procedure above is a single-pass STRIDE analysis with ZERO self-verification: one analyst, one draft, no adversary to probe what was missed. To close that gap, the caller (meta, or any session that can spawn workers) may follow the analysis with ONE red-team review. threat-model's own invocation is the STRIDE analysis pass only; it never reviews itself.

1. **Persist**: the threat model is written to disk first. Every asset and flow must be evaluated against all six STRIDE categories, every open threat rated and given a mitigation; a model that skips a category, leaves a flow unrated, or leaves a critical unmitigated is not complete. A producer worker returns `STATUS: DONE`; it never self-certifies its own coverage.
2. **Review (red-team)**: dispatch `w-hostile-reviewer` once. Its adversarial hostile-review gauntlet is a reasonable red-team auditor for a threat model: it maps cleanly onto hunting the attack surface the producer missed. It receives the threat model + rubric ONLY (reviewer isolation), examines the CURRENT model with fresh evidence (no pre-approval), and probes for unmapped attack surface, absent STRIDE categories, under-rated severities, and unmitigated criticals. Line 1 of its return is `VERDICT: REWORK|CLEAN blocking=N major=N minor=N`.
3. **Report**: the caller quotes the reviewer's `VERDICT:` line verbatim. Only the reviewer authors the token.
4. **Triage**: accept or contest each finding with evidence (file:line, the STRIDE category the surface falls under, or a concrete exploit path); accepted findings are worked into the threat model, contested ones are logged with a rebuttal. Then stop. A further red-team pass happens only if the owner or meta judges it necessary.

## Cross-References

- Token protocol and severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Red-team gauntlet (run by `w-hostile-reviewer`): `~/.claude/skills/hostile-review/SKILL.md`
- Dispatch contract for a producer worker: `~/.claude/skills/_shared/dispatch-contract.md`
