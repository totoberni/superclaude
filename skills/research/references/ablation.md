> Part of /research (see ../SKILL.md). Subcommand: ablation.

## `ablation` -- Design ablation study

**Args**: `ablation <component> [--baseline <EXP-ID>] [--budget <N>]`

1. Read component code + `experiments.md` for baseline
2. Identify parameters and sub-components
3. For EACH ablation: state the hypothesis, the isolated variable, and
   the confounder controls (not just "change X and see what happens").
   Inherits `design`'s pre-registration and triangulation requirements
   (Principles 6, 11) -- an ablation without a pre-registered hypothesis
   and a triangulation plan is exploratory, not ablative.
4. Output: `| # | What to Change | Hypothesis | Config Change | Priority |`
5. Budget (default 5) limits runs. Prioritize by information gain.
6. Include compute estimate (runs x time per run)
