# _shared/discovery-protocol.md: project discovery protocol (SOT)

Consumed by: /research (all subcommands except `paper` with `--in` explicit). Reusable by any project-context-aware skill.

The skill does not hard-code per-project paths. Every subcommand operating on an existing project context runs this protocol at startup and states each resolution explicitly at the top of its output or working notes.

1. **Project root**: the nearest ancestor directory of the caller's CWD containing a git repository, a `docs/` folder, or a `~/.claude/plans/<name>/plan.md` entry.

2. **Project memory**: query the DB for accumulated traps: `memory_db.py search '<name> gotchas mistakes wins' -k 8` or `list --tier shared-projects`. A review/design/gap audit is the right moment to verify none were silently re-introduced. (For SELECTING a bounded briefing subset rather than a full recall, see [memory-distill.md](memory-distill.md).)

3. **Markscheme**: prefer `~/.claude/plans/<project>/markscheme.md`, fall back to `<project-root>/docs/markscheme.md`, fall back to `--markscheme <path>`. None found -> mode defaults to A; state "no markscheme discovered" and apply only the principle-level gauntlet -- do not invent tier weights.

4. **Spec or syllabus**: look in `<project-root>/docs/*.pdf` and `*.tex` for the brief/CFP/task spec, or `--spec <path>` if supplied. Read in full; enumerate explicit requirements as a checklist the subcommand audits coverage against.

5. **Product under review**: coursework -> report + notebook + code modules; research project -> design document + code under analysis + reference paper; reproducibility study -> reproduction repo + divergence log + paper PDF. State the enumerated product list explicitly; findings must cite locations within this set.

6. **Prior reviews and decision logs**: `<project-root>/docs/phase*-hostile-review.md`, `docs/*review*.md`, `~/.claude/comms/*hrev*/review.md`. If a decision log exists (`state.md`, `context.md`, `decisions.md`), read it for pre-registered choices. Prior reviews exist -> ask what they missed; don't re-raise an addressed finding without new evidence the fix was incomplete.

7. **Output path and mode**: resolve output path per the subcommand's conventions; resolve mode per mode-detection (markscheme found -> B, none -> A, ambiguous -> A with markscheme alignment as an extra constraint).

## Note

Mode A/B is research-specific (rigour tiers). A consuming skill without that concept keeps steps 1-6 verbatim and drops step 7's mode clause.

## Cross-reference

Literature-search steps invoked from step 4/5 follow [search-budget.md](search-budget.md)'s budget discipline.
