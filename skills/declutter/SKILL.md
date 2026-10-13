---
name: declutter
description: "Use when tidying a finished project: git safety net, delete runtime and intermediate files, optionally zip sources"
category: workflow
user-invocable: true
argument-hint: "<repo-path> [--zip]"
allowed-tools: Read, Write, Edit, Bash, PowerShell, Glob, Grep, Agent
---

# /declutter: Tidy a Finished Project

Shrink a finished project to code + sources + deliverables, with a git safety net so every deletion is recoverable and a reproducibility proof so nothing needed is lost.

**Arguments**: $ARGUMENTS (`<repo-path>` required; `--zip` bundles source inputs into one zip)

Heavy lifting goes to a worker: meta dispatches a `w-implementer` (opus, per the >20 tool-call rule) with this procedure and the repo path, then verifies per R-3 (read the diff, re-run the md5 check, re-check the tree). Meta-direct work is limited to small polish.

## Step 0: Preconditions

1. The owner explicitly authorised deletion THIS session. If not, stop and ask.
2. Read `~/.claude/config/git-policy`. If it says `disabled`, stop and ask the owner to run `/git true`. Never toggle it yourself unprompted.
3. Identify the **deliverables** (never deleted). Record an md5 snapshot of all of them now; compare after every step.
4. Identify the **minimal input set** that reproduces the deliverables. Prefer an existing packaging script or manifest as SOT (e.g. a `package_code.py` file list) over inventing a list.

## Step 1: Safety net

- Not a git repo: `git init`, `git config core.autocrlf false`, minimal `.gitignore` (`.venv`, `__pycache__`), then ONE snapshot commit of EVERYTHING, caches and intermediates included, so every deletion is recoverable.
- Already a repo: commit or stash nothing of the owner's without asking. Work on a branch only if the owner says so.
- Caveat to tell the owner: the snapshot roughly doubles disk use until `.git` is removed.

## Step 2: Classify

Classify every top-level dir and every large file:

| Class | Examples | Action |
|---|---|---|
| deliverable | final reports, versioned outputs | keep, md5-guard |
| code | scripts, package, configs | keep |
| source input | raw data, manual reviews, overrides | keep (zip with `--zip`) |
| runtime | venv, caches, `__pycache__`, build dirs, logs | delete (venv last) |
| intermediate | derived data, `tmp_*`, scratch, download caches | delete |

Show the table when anything is ambiguous and ask before deleting ambiguous items.

## Step 3: Zip sources (only with `--zip`)

Bundle all source inputs, plus any non-regenerable work inputs (manual reviews, overrides, hand corrections), into ONE zip under `source/`. Paths are relative to the repo root so unzipping at the root restores them. Without `--zip`, leave sources as they are.

## Step 4: Prove before deleting

Build a fresh temp tree containing only code + (unzipped) sources. Rebuild the outputs there with the existing env and compare key outputs to the current ones. Required: 0 mismatches. A file list is not proof; a full rerun is. If not reproducible: stop, report, delete nothing.

## Step 5: Delete

Delete runtime and intermediate files. Delete the virtual environment LAST (Step 4 needs it). Update `.gitignore`: runtime dirs, build artefacts, extracted sources (except the zip).

## Step 6: Structure check and iterate

Make the tree as flat and simple as possible: code / source / deliverables / README / requirements. Fix README setup and structure sections to match (install env, unzip sources); keep the project's own language rules (an Italian README stays Italian). Repeat until the tree is clean. Commit after each step with conventional messages (`chore:` / `docs:`) plus the session's co-author line. Re-run the md5 check on deliverables.

## Step 7: Stop and ask for review

Report: commits, size before/after (excluding `.git`), `.git` size, final tree, reproducibility result, md5 check of deliverables. Then STOP and wait for the owner.

## Step 8: After review

Delete `.git` ONLY if the owner explicitly asks (it served its purpose as safety net); otherwise leave it. Never push.

## Examples

**ExcITA** (Nomisma / Comitato Leonardo Made in Italy scoring, `C:\Users\berni.alberto\code\ExcITA`, 2026-10-13), run with `--zip`:

- Before: 3.6 GB (`work/` 3.0 GB incl. a 2.3 GB RNA download cache, `.venv` 230 MB, `source/` 113 MB, `deliverables/` 237 MB, `docs/` one xlsx).
- Safety snapshot commit `e277a8e` (`.git` 2.7 GB).
- `source/ExcITA_source.zip`: 609 files, 67.8 MB: all of `source/`, the docs xlsx, and the non-regenerable work inputs listed by `scripts/package_code.py` (manual reviews, batches, overrides, v1 base corrections).
- Proof: fresh temp tree, `run_all.py`, `scored.parquet` compared on codice fiscale (161,281 rows): 0 mismatches.
- Deleted `work/`, `.venv` (last), `docs/`, loose source files, report build dirs; deliverables v1/v2/v3 md5 unchanged.
- After: 301 MB at rest; README install = venv + pip + unzip at root; `.gitignore` ignores extracted sources except the zip.
- Owner reviewed, kept `.git`.

**Illustrative only** (generic Python ML repo, no `--zip`): delete checkpoints, `wandb/`, `runs/` caches and `__pycache__`; keep configs, code, and the final model card. Sources stay in place; the proof is a short rerun of the eval from a fresh tree against the kept final model.

## Pitfalls

- The snapshot commit keeps deleted data in `.git`: real disk saving only after `.git` is removed.
- Prove the source zip with a full rerun before deleting anything; a file list is not proof.
- Delete the venv last; the proof needs it.
- Extracted sources show as untracked unless ignored.
- Never delete deliverables of earlier versions; check md5 before and after.
- Version-named outputs: retarget output paths before rerunning so archives are not overwritten.
