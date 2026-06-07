---
name: figure-validate
description: "Pre-flight figure validator: WCAG-AA contrast, thin lines, low alpha."
model: haiku
category: code-quality
user-invocable: true
disable-model-invocation: true
argument-hint: "FIGURE [FIGURE ...] [--mode image|code|pdf] [--min-contrast N] [--min-linewidth N] [--min-alpha N] [--json]"
allowed-tools: Bash, Read
---

# Figure Pre-Flight Validator

Flags publication-quality legibility defects in scientific figures (matplotlib /
ML-paper plots) before they reach a paper or slide deck. Three checks, two input
modes. It is a **reporting** tool: exit code is 0 whether or not defects are
found — a non-zero exit means the tool itself failed (bad path, unreadable file).

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

Run via the dedicated superclaude venv (absolute path — venv discipline):

```bash
# Validate a rendered figure.
~/.claude/.venv/bin/python ~/.claude/scripts/figure-validate.py figure.png

# Validate the plotting code that produced it (static scan).
~/.claude/.venv/bin/python ~/.claude/scripts/figure-validate.py make_fig.py

# Mix modes, machine-readable output.
~/.claude/.venv/bin/python ~/.claude/scripts/figure-validate.py --json plot.py fig.png

# Override thresholds (e.g. stricter linewidth for a print journal).
~/.claude/.venv/bin/python ~/.claude/scripts/figure-validate.py \
    --min-contrast 7.0 --min-linewidth 1.0 figure.png
```

**Args**: positional `FIGURE` paths (one or more); `--mode {auto,image,code,pdf}`
(default `auto`); `--min-contrast` (default `4.5`); `--min-linewidth` (default
`0.8`); `--min-alpha` (default `0.3`); `--json` for structured output.

## Output

Per-file `[pass]` / `[FLAG]` header, then one line per issue with severity
(`HIGH`/`MED`/`ok`), check kind, `file:line` (code) or `file (image)` location,
a description, and a `fix:` suggestion. Closes with a `summary:` line counting
flagged files and total findings.

## Dependencies

Needs `Pillow` + `numpy` (present in `~/.claude/.venv`). `PyMuPDF` is optional
(enables PDF mode). `matplotlib` is only needed to regenerate the self-test
sample figures below — the validator itself never imports it. Missing deps are
listed in `~/.claude/dependencies.yml` (owner installs; agents do not pip-install).

## Self-test (after `pip install matplotlib`)

```bash
~/.claude/.venv/bin/python - <<'PY'
import subprocess, tempfile, os
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, numpy as np
V = os.path.expanduser("~/.claude/.venv/bin/python")
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
PY
```

Expected: `clean.png` → `[pass]`; `bad.png` → `[FLAG]` (low contrast).
