> Part of /research (see ../SKILL.md). Subcommand: reproduce.

## `reproduce` -- Check reproduction fidelity

**Args**: `reproduce <paper> [--strict]`

1. Read architecture, training loop, data pipeline
2. Build checklist: Architecture, Training, Data
3. Score: X/Y match per category
4. Flag critical gaps + acceptable deviations
5. `--strict` flags ALL deviations; default only critical gaps
6. **Divergence log**: cross-reference `docs/reprod-notes.md`. Each entry:
   what changed, why, and impact assessment.
