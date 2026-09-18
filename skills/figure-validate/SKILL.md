---
name: figure-validate
description: "Use when validating figures for WCAG-AA contrast, thin lines, low alpha"
model: haiku
category: code-quality
user-invocable: true
argument-hint: "FIGURE [FIGURE ...] [--mode image|code|pdf] [--min-contrast N] [--min-linewidth N] [--min-alpha N] [--json]"
allowed-tools: Bash, Read
---

# Figure Pre-Flight Validator

Flags publication-quality legibility defects in scientific figures (matplotlib /
ML-paper plots) before they reach a paper or slide deck. Three checks, two input
modes. It is a **reporting** tool: exit code is 0 whether or not defects are
found; a non-zero exit means the tool itself failed (bad path, unreadable file).
The check is deterministic, so it can also gate a fix-and-revalidate pass
(see Gate semantics): fix, then re-run until zero figures are flagged.

## Checks

| Check | Threshold (default) | Rule |
|-------|---------------------|------|
| WCAG-AA contrast | `>= 4.5:1` | foreground vs background relative-luminance contrast ratio |
| Linewidth | `>= 0.8 pt` | lines thinner than this drop out in print / on projectors |
| Alpha | `>= 0.3` | more transparent than this washes elements into the page |

The contrast check uses the **real W3C WCAG formula**: sRGB → linearize each
channel (`c/12.92` if `c<=0.03928` else `((c+0.055)/1.055)^2.4`) →
`L = 0.2126R + 0.7152G + 0.0722B` → ratio `(L1+0.05)/(L2+0.05)`. Verified exact
against reference values (black/white = 21:1, `#777` on white = 4.48:1).

## Input modes (auto-detected by extension; override with `--mode`)

- **image** (`.png/.jpg/.jpeg/.bmp/.tif/.tiff/.gif`) — analyses pixels for the
  dominant background and the most salient foreground colour, reports their
  contrast ratio.
- **code** (`.py`) — static AST scan for `linewidth=`/`lw=`, `alpha=`, and
  `color=`/`c=`/`edgecolor=`/`facecolor=` pairs; evaluates each against the
  thresholds (colours resolved via hex / named / grayscale-float / tableau).
- **pdf** (`.pdf`) — rasterises page 1 if PyMuPDF (`fitz`) is installed; reported
  as a graceful **skip** (not an error) if the backend is absent.

## Usage

Run via the `scpy` shim (dedicated superclaude venv — venv discipline):

```bash
# Validate a rendered figure.
scpy figure-validate.py figure.png

# Validate the plotting code that produced it (static scan).
scpy figure-validate.py make_fig.py

# Mix modes, machine-readable output.
scpy figure-validate.py --json plot.py fig.png

# Override thresholds (e.g. stricter linewidth for a print journal).
scpy figure-validate.py --min-contrast 7.0 --min-linewidth 1.0 figure.png
```

**Args**: positional `FIGURE` paths (one or more); `--mode {auto,image,code,pdf}`
(default `auto`); `--min-contrast` (default `4.5`); `--min-linewidth` (default
`0.8`); `--min-alpha` (default `0.3`); `--json` for structured output.

## Output

Per-file `[pass]` / `[FLAG]` header, then one line per issue with severity
(`HIGH`/`MED`/`ok`), check kind, `file:line` (code) or `file (image)` location,
a description, and a `fix:` suggestion. Closes with a `summary:` line counting
flagged files and total findings.

## Gate semantics and fix-and-revalidate

`figure-validate` is a DETERMINISTIC gate, not an LLM reviewer. Each run emits
per-file `[pass]` / `[FLAG]` lines with concrete `fix:` suggestions; it never
emits a `VERDICT` token. Under the two-token protocol its result maps
through the deterministic-checker row of the severity table
(`_shared/verdict-schema.md`): every `[FLAG]` is a failed-gate (blocking-class)
finding, with no major/minor gradation. The per-issue `HIGH`/`MED` ordering only
prioritises which flag to fix first; for the gate, ANY `[FLAG]` fails.
Because the gate is deterministic it needs no LLM auditor: the validator's
own `0 [FLAG]` report on the latest figures IS the acceptance evidence.

When a run reports `[FLAG]` findings:

1. **FIX**: for every `[FLAG]`, apply its `fix:` suggestion by
   regenerating the figure with the corrected setting (thicker linewidth, higher
   alpha, higher-contrast colour). Delegate the regeneration to a producer per
   `dispatch-contract.md` (a trivial code-mode edit may be done inline). Producers
   never self-certify the result.
2. **RE-VALIDATE**: the parent (meta or the owner's session) RE-RUNS the plain
   validator itself over ALL target figures (tool-verified critique: never accept
   a producer's "I fixed it"). Read the flagged-file count from the `summary:`
   line, or from `--json` for a machine-readable count. The exit code stays 0, so
   the `[FLAG]` count, not the exit code, is the signal.

Acceptance needs two independent signals: a `0 [FLAG]` validator report on the
latest revision AND the producer's separate `STATUS: DONE`. A `0 [FLAG]` report
from before the last regeneration is stale evidence and never counts (no
pre-approval; `verdict-schema.md`, No pre-approval). If the `[FLAG]` count does
not fall after a fix attempt, stop and report to the owner rather than retrying
blindly.

## Dependencies

Needs `Pillow` + `numpy` (present in `~/.claude/.venv`). `PyMuPDF` is optional
(enables PDF mode). `matplotlib` is only needed to regenerate the self-test
sample figures below — the validator itself never imports it. Missing deps are
listed in `~/.claude/dependencies.yml` (owner installs; agents do not pip-install).

## Self-test (after the owner installs `matplotlib` into `~/.claude/.venv` with `uv`)

Write the script below to `~/.claude/tmp/figure_selftest.py` with the Write tool,
then run `scpy ~/.claude/tmp/figure_selftest.py`:

```python
import subprocess, sys, tempfile, os
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, numpy as np
V = sys.executable  # the ~/.claude/.venv interpreter that scpy runs under
S = os.path.expanduser("~/.claude/scripts/figure-validate.py")
t = Path(tempfile.mkdtemp()); x = np.linspace(0, 10, 200)
# clean: thick dark line on white
f, a = plt.subplots(); a.plot(x, np.sin(x), color="black", linewidth=2.0)
f.savefig(t / "clean.png", facecolor="white"); plt.close(f)
# bad: thin, near-invisible pale line on near-white
f, a = plt.subplots(facecolor="#eeeeee")
a.set_facecolor("#eeeeee")
a.plot(x, np.sin(x), color="#dddddd", linewidth=0.3, alpha=0.2)
f.savefig(t / "bad.png", facecolor="#eeeeee"); plt.close(f)
for name in ("clean.png", "bad.png"):
    print(subprocess.run([V, S, str(t / name)], capture_output=True, text=True).stdout)
import shutil; shutil.rmtree(t)
```

Expected: `clean.png` → `[pass]`; `bad.png` → `[FLAG]` (low contrast).

## Cross-References

- Two-token protocol and severity mapping (deterministic-checker row:
  `figure-validate` FLAG = failed gate): `~/.claude/skills/_shared/verdict-schema.md`
- Dispatch contract and model split (producer regeneration budgets):
  `~/.claude/skills/_shared/dispatch-contract.md`
