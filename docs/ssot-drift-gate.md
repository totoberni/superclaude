# SSOT Drift Gate

A registered concept has exactly one canonical home. A re-definition, contradiction, or broken
pointer anywhere else in the corpus fails a mechanical gate: DRY and no-restatement (see
`rules/15-programming-principles.md` § 1) are enforced, not just advised.

## The registry

`meta/concept-registry.yaml` is the SSOT-of-SSOTs, consumed by `scripts/ssot-lint.py`. It is a
small hand-rolled YAML subset (stdlib-only, no pyyaml, so the linter runs on a pre-commit path),
grammar documented in the file's own header comment.

| Field | Meaning |
|---|---|
| `id` | Bare slug, unique per record. |
| `canonical_home` | `path.ext` or `path.ext:Anchor Heading Text`; path relative to `--root`. |
| `defining_marker` | Regex that must match ONLY inside `canonical_home` across the corpus; a hit elsewhere is a re-definition. |
| `forbidden_pattern` | Regex that must have ZERO hits outside `canonical_home`; for contradictions or retired claims that must not reappear. |
| `ground_truth` | Shell command (via `sh -c`) asserted to exit 0; used when the invariant is a filesystem/corpus fact, not a designated home. |
| `pointer_only` | Bool, default false. When true, only `canonical_home` resolution is checked; no marker, pattern, or ground-truth check runs. |

A `defining_marker` anchored to a heading line (`^#{1,6}\s+...`) lets a prose citation of that
heading (`see rules/13 § Foo`) pass as a legitimate pointer instead of false-positiving as a
re-definition; only an actual second heading line counts as re-defined.

## The linter

`scripts/ssot-lint.py` runs, per registered concept: `canonical_home` resolves (file exists, and
its anchor, if given, is a real heading in it); `defining_marker` matches only inside the home;
`forbidden_pattern` has zero hits outside the home; `ground_truth` exits 0. It also runs a generic
pointer resolver over the whole corpus: every `` SOT: `path` `` and `` see `path` `` reference
found anywhere must resolve to a real file.

Invocation: `python3 ~/.claude/scripts/ssot-lint.py --root ~/.claude`. It reports failures, exit 1
on any, exit 0 clean. As of this writing there is one known pre-existing benign unresolved
pointer, `skills/research/references/report.md:456`, out of scope of this campaign and not new
drift; every other check is clean.

## Adding a concept

1. Append a record to `meta/concept-registry.yaml`: `id`, `canonical_home`, and one detection
   mechanism.
2. Pick the mechanism: `defining_marker` for a concept with a canonical, quotable definition;
   `forbidden_pattern` for a retired or wrong claim that must never reappear outside the home;
   `ground_truth` for a fact about the filesystem or corpus rather than a designated home;
   `pointer_only` when no marker or pattern can be written without being fragile or
   false-positive-prone (no clean machine-checkable signal exists).
3. Re-run the linter and confirm zero NEW failures (the one known benign pointer above is the only
   pre-existing exception).

When the marker is a heading, anchor it to the heading line itself
(`^#{1,6}\s+Exact Heading Text`) rather than a bare substring, so other files citing that heading
in prose are not mistaken for a competing definition.

Bite tests: `scripts/tests/test-ssot-lint.sh` (self-contained, builds fixtures under `mktemp -d`,
never touches `~/.claude`; covers clean pass, re-defined marker, forbidden-pattern hit, broken
pointer, and ground-truth mismatch).
