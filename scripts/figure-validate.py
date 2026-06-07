#!/usr/bin/env python3
"""
figure-validate.py — publication-figure pre-flight validator.

Flags three classes of legibility defect in scientific figures (matplotlib /
ML-paper plots), in either of two input modes:

  1. WCAG-AA contrast  — foreground vs background contrast ratio < 4.5:1, using
     the real W3C relative-luminance + contrast-ratio formula.
  2. Linewidth too thin — lines below a legibility threshold (default 0.8 pt).
  3. Alpha too transparent — alpha below a threshold (default 0.3) that washes
     elements out against the page.

Input modes (auto-detected from extension, or forced with --mode):
  - image (.png/.jpg/.jpeg/.bmp/.tif/.tiff): analyse pixels for dominant
    background vs salient foreground, report their contrast ratio.
  - code  (.py): static-scan source for linewidth=/lw=/alpha= kwargs and
    color=/c= + facecolor/edgecolor pairs, evaluating each against thresholds
    and (for color pairs) WCAG contrast.
  - pdf   (.pdf): only if a rasterising backend (pymupdf/`fitz`) is available;
    otherwise reported as a skipped check (NOT an error).

This is a REPORTING tool: it exits 0 whether or not it finds issues. A non-zero
exit indicates the tool itself failed (bad path, unreadable file, missing dep
for the requested mode).

Usage:
    ~/.claude/.venv/bin/python ~/.claude/scripts/figure-validate.py FIGURE [FIGURE ...]
    ~/.claude/.venv/bin/python ~/.claude/scripts/figure-validate.py plot.py fig.png
    ~/.claude/.venv/bin/python ~/.claude/scripts/figure-validate.py --mode code plot.py
    ~/.claude/.venv/bin/python ~/.claude/scripts/figure-validate.py --json fig.png
    ~/.claude/.venv/bin/python ~/.claude/scripts/figure-validate.py \
        --min-contrast 4.5 --min-linewidth 0.8 --min-alpha 0.3 fig.png
"""

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# ── Defaults ──────────────────────────────────────────────────────────────
# WCAG 2.x AA requires >= 4.5:1 for normal text / fine graphical detail.
DEFAULT_MIN_CONTRAST = 4.5
# matplotlib default linewidth is 1.5 pt; sub-~0.8 pt hairlines drop out in
# print and on projectors.
DEFAULT_MIN_LINEWIDTH = 0.8
# Alpha below ~0.3 washes elements into the background.
DEFAULT_MIN_ALPHA = 0.3

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif"}
CODE_EXTS = {".py"}
PDF_EXTS = {".pdf"}

# Severity ordering for sorting / summarising.
SEV_ORDER = {"error": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _is_finding(issue) -> bool:
    """An actionable finding: any issue that is not a passing/skipped info note.

    `info` severity == a check that passed or was inapplicable (`skipped`);
    everything else (error/high/medium/low) is something the user should act on.
    """
    return issue.severity != "info" and issue.kind != "skipped"


# ── WCAG relative luminance + contrast ratio (canonical W3C formula) ────────
def _linearize_channel(c8: float) -> float:
    """sRGB 8-bit channel value [0,255] -> linear-light component [0,1]."""
    cs = c8 / 255.0
    if cs <= 0.03928:
        return cs / 12.92
    return ((cs + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb) -> float:
    """
    WCAG relative luminance of an sRGB colour.

    rgb: (R, G, B) with each channel in [0, 255].
    L = 0.2126*R_lin + 0.7152*G_lin + 0.0722*B_lin  (W3C definition).
    """
    r, g, b = rgb[0], rgb[1], rgb[2]
    rl = _linearize_channel(r)
    gl = _linearize_channel(g)
    bl = _linearize_channel(b)
    return 0.2126 * rl + 0.7152 * gl + 0.0722 * bl


def contrast_ratio(rgb1, rgb2) -> float:
    """
    WCAG contrast ratio between two sRGB colours: (L_light + 0.05)/(L_dark + 0.05).
    Ranges from 1:1 (identical) to 21:1 (pure black vs pure white).
    """
    l1 = relative_luminance(rgb1)
    l2 = relative_luminance(rgb2)
    lighter, darker = (l1, l2) if l1 >= l2 else (l2, l1)
    return (lighter + 0.05) / (darker + 0.05)


# ── Colour-spec parsing (for code mode) ─────────────────────────────────────
# A curated subset of CSS / matplotlib named colours -> 8-bit RGB. Covers the
# names that realistically appear as plot colours; unknown names are reported
# as "unresolved" rather than guessed.
_NAMED_COLORS = {
    "black": (0, 0, 0), "k": (0, 0, 0),
    "white": (255, 255, 255), "w": (255, 255, 255),
    "red": (255, 0, 0), "r": (255, 0, 0),
    "green": (0, 128, 0), "g": (0, 128, 0),
    "lime": (0, 255, 0),
    "blue": (0, 0, 255), "b": (0, 0, 255),
    "cyan": (0, 255, 255), "c": (0, 255, 255),
    "magenta": (255, 0, 255), "m": (255, 0, 255),
    "yellow": (255, 255, 0), "y": (255, 255, 0),
    "gray": (128, 128, 128), "grey": (128, 128, 128),
    "lightgray": (211, 211, 211), "lightgrey": (211, 211, 211),
    "darkgray": (169, 169, 169), "darkgrey": (169, 169, 169),
    "silver": (192, 192, 192),
    "gainsboro": (220, 220, 220),
    "orange": (255, 165, 0),
    "purple": (128, 0, 128),
    "brown": (165, 42, 42),
    "pink": (255, 192, 203),
    "navy": (0, 0, 128),
    "teal": (0, 128, 128),
    "olive": (128, 128, 0),
    "maroon": (128, 0, 0),
    # matplotlib tableau defaults (the C0..C9 cycle).
    "tab:blue": (31, 119, 180), "c0": (31, 119, 180),
    "tab:orange": (255, 127, 14), "c1": (255, 127, 14),
    "tab:green": (44, 160, 44), "c2": (44, 160, 44),
    "tab:red": (214, 39, 40), "c3": (214, 39, 40),
    "tab:purple": (148, 103, 189), "c4": (148, 103, 189),
    "tab:brown": (140, 86, 75), "c5": (140, 86, 75),
    "tab:pink": (227, 119, 194), "c6": (227, 119, 194),
    "tab:gray": (127, 127, 127), "c7": (127, 127, 127),
    "tab:olive": (188, 189, 34), "c8": (188, 189, 34),
    "tab:cyan": (23, 190, 207), "c9": (23, 190, 207),
}


def parse_color(spec) -> Optional[tuple]:
    """
    Best-effort parse of a matplotlib colour spec to (R, G, B) in [0, 255].
    Returns None if the spec cannot be resolved without matplotlib (e.g. an
    unknown name, a colormap reference, or a non-literal expression).
    """
    if spec is None:
        return None
    # Numeric grayscale string, e.g. "0.5".
    if isinstance(spec, (int, float)):
        v = int(round(float(spec) * 255))
        v = max(0, min(255, v))
        return (v, v, v)
    if not isinstance(spec, str):
        return None
    s = spec.strip().lower()
    if not s:
        return None
    # Hex: #rgb, #rrggbb, #rrggbbaa.
    if s.startswith("#"):
        h = s[1:]
        try:
            if len(h) == 3:
                return tuple(int(ch * 2, 16) for ch in h)
            if len(h) in (6, 8):
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except ValueError:
            return None
        return None
    # Grayscale float-as-string, e.g. "0.5".
    try:
        f = float(s)
        v = max(0, min(255, int(round(f * 255))))
        return (v, v, v)
    except ValueError:
        pass
    return _NAMED_COLORS.get(s)


# ── Issue record ────────────────────────────────────────────────────────────
@dataclass
class Issue:
    severity: str          # error | high | medium | low | info
    kind: str              # contrast | linewidth | alpha | skipped | error
    location: str          # "file:line" or "file (image)"
    message: str
    suggestion: str = ""
    detail: dict = field(default_factory=dict)


# ── Image mode ────────────────────────────────────────────────────────────
def _dominant_and_foreground(img, max_dim=400):
    """
    Reduce an RGB image to (background_rgb, foreground_rgb).

    Background = the single most frequent quantised colour (the page / axes
    fill). Foreground = the quantised colour, among the rest, that is most
    distant in luminance from the background and occupies a non-negligible
    area (markers, lines, text). Returns 8-bit RGB tuples.
    """
    import numpy as np

    # Downscale for speed; nearest keeps colours crisp (no interpolation halos).
    w, h = img.size
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    arr = np.asarray(img, dtype=np.uint8).reshape(-1, 3)
    # Quantise to 4 bits/channel (16 levels) so anti-aliased edges collapse
    # onto their parent colour instead of fragmenting the histogram.
    q = (arr >> 4).astype(np.uint32)
    keys = (q[:, 0] << 8) | (q[:, 1] << 4) | q[:, 2]
    uniq, counts = np.unique(keys, return_counts=True)

    def key_to_rgb(k):
        r = ((k >> 8) & 0xF) * 17  # 0..15 -> 0..255 (×17 == ×255/15)
        g = ((k >> 4) & 0xF) * 17
        b = (k & 0xF) * 17
        return (int(r), int(g), int(b))

    order = np.argsort(counts)[::-1]
    bg_key = int(uniq[order[0]])
    bg_rgb = key_to_rgb(bg_key)
    bg_lum = relative_luminance(bg_rgb)
    total = int(counts.sum())

    # Foreground candidate: maximise luminance distance from bg, weighted so a
    # colour must hold >=0.5% of pixels to count (ignores stray JPEG noise).
    best_rgb = None
    best_score = -1.0
    for idx in order:
        k = int(uniq[idx])
        if k == bg_key:
            continue
        frac = counts[idx] / total
        if frac < 0.005:
            continue
        rgb = key_to_rgb(k)
        lum_dist = abs(relative_luminance(rgb) - bg_lum)
        if lum_dist > best_score:
            best_score = lum_dist
            best_rgb = rgb

    if best_rgb is None:
        # Degenerate (near-solid image): fall back to the 2nd most frequent.
        if len(order) > 1:
            best_rgb = key_to_rgb(int(uniq[order[1]]))
        else:
            best_rgb = bg_rgb
    return bg_rgb, best_rgb


def validate_image(path: Path, cfg) -> list:
    issues = []
    try:
        from PIL import Image
    except ImportError:
        issues.append(Issue(
            "error", "error", f"{path.name} (image)",
            "Pillow not installed — cannot analyse image pixels.",
            "Add 'Pillow' to ~/.claude/requirements.txt and install into the venv.",
        ))
        return issues

    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            bg, fg = _dominant_and_foreground(im)
    except Exception as exc:  # noqa: BLE001 — surface any decode failure as tool error
        issues.append(Issue(
            "error", "error", f"{path.name} (image)",
            f"Failed to read image: {exc}", "",
        ))
        return issues

    ratio = contrast_ratio(fg, bg)
    if ratio < cfg.min_contrast:
        sev = "high" if ratio < cfg.min_contrast * 0.78 else "medium"
        issues.append(Issue(
            sev, "contrast", f"{path.name} (image)",
            f"Dominant foreground/background contrast {ratio:.2f}:1 "
            f"< {cfg.min_contrast}:1 (WCAG-AA).",
            f"Darken the foreground or lighten the background. fg=rgb{fg}, "
            f"bg=rgb{bg}.",
            {"ratio": round(ratio, 3), "fg": list(fg), "bg": list(bg)},
        ))
    else:
        issues.append(Issue(
            "info", "contrast", f"{path.name} (image)",
            f"Foreground/background contrast {ratio:.2f}:1 OK "
            f"(>= {cfg.min_contrast}:1).",
            "", {"ratio": round(ratio, 3), "fg": list(fg), "bg": list(bg)},
        ))
    return issues


# ── PDF mode ────────────────────────────────────────────────────────────────
def validate_pdf(path: Path, cfg) -> list:
    issues = []
    try:
        import fitz  # PyMuPDF
    except ImportError:
        issues.append(Issue(
            "info", "skipped", f"{path.name} (pdf)",
            "PDF rasterisation backend (PyMuPDF / 'fitz') not installed — "
            "PDF contrast check skipped.",
            "Add 'PyMuPDF' to ~/.claude/requirements.txt to enable PDF mode, "
            "or export the figure to PNG.",
        ))
        return issues

    try:
        from PIL import Image
        import io
        doc = fitz.open(path)
        page = doc.load_page(0)
        pix = page.get_pixmap(dpi=150)
        im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        doc.close()
        bg, fg = _dominant_and_foreground(im)
    except Exception as exc:  # noqa: BLE001
        issues.append(Issue(
            "error", "error", f"{path.name} (pdf)",
            f"Failed to rasterise PDF: {exc}", "",
        ))
        return issues

    ratio = contrast_ratio(fg, bg)
    if ratio < cfg.min_contrast:
        sev = "high" if ratio < cfg.min_contrast * 0.78 else "medium"
        issues.append(Issue(
            sev, "contrast", f"{path.name} (pdf, page 1)",
            f"Dominant foreground/background contrast {ratio:.2f}:1 "
            f"< {cfg.min_contrast}:1 (WCAG-AA).",
            f"Darken the foreground or lighten the background. fg=rgb{fg}, "
            f"bg=rgb{bg}.",
            {"ratio": round(ratio, 3), "fg": list(fg), "bg": list(bg)},
        ))
    else:
        issues.append(Issue(
            "info", "contrast", f"{path.name} (pdf, page 1)",
            f"Foreground/background contrast {ratio:.2f}:1 OK.",
            "", {"ratio": round(ratio, 3)},
        ))
    return issues


# ── Code mode (static AST scan) ──────────────────────────────────────────────
# kwargs whose value is a colour, grouped so we can pair fg vs bg for contrast.
_FG_COLOR_KW = {"color", "c", "edgecolor", "ec", "markeredgecolor", "mec"}
_BG_COLOR_KW = {"facecolor", "fc", "markerfacecolor", "mfc"}
_LINEWIDTH_KW = {"linewidth", "lw"}
_ALPHA_KW = {"alpha"}


def _literal(node):
    """Return the Python literal value of an AST node, or None if non-literal."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        # Handle a bare unary minus on a constant (older ASTs) gracefully.
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = _literal(node.operand)
            if isinstance(inner, (int, float)):
                return -inner
        return None


def _func_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return "<call>"


def validate_code(path: Path, cfg) -> list:
    issues = []
    try:
        src = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        issues.append(Issue("error", "error", path.name,
                            f"Failed to read file: {exc}", ""))
        return issues

    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        issues.append(Issue(
            "error", "error", f"{path.name}:{exc.lineno or '?'}",
            f"Python syntax error — cannot static-scan: {exc.msg}", "",
        ))
        return issues

    found_any = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = _func_name(node)
        line = node.lineno
        # Collect this call's relevant kwargs.
        fg_specs, bg_specs = [], []
        for kw in node.keywords:
            if kw.arg is None:  # **kwargs splat — skip.
                continue
            name = kw.arg.lower()
            val = _literal(kw.value)

            if name in _LINEWIDTH_KW:
                found_any = True
                if isinstance(val, (int, float)):
                    if val < cfg.min_linewidth:
                        issues.append(Issue(
                            "medium", "linewidth", f"{path.name}:{line}",
                            f"{fname}(..., {kw.arg}={val}) below "
                            f"{cfg.min_linewidth}pt legibility threshold.",
                            f"Raise {kw.arg} to >= {cfg.min_linewidth} "
                            f"(matplotlib default is 1.5).",
                            {"value": val, "func": fname},
                        ))
            elif name in _ALPHA_KW:
                found_any = True
                if isinstance(val, (int, float)):
                    if val < cfg.min_alpha:
                        issues.append(Issue(
                            "medium", "alpha", f"{path.name}:{line}",
                            f"{fname}(..., {kw.arg}={val}) below "
                            f"{cfg.min_alpha} transparency threshold — washes out.",
                            f"Raise {kw.arg} to >= {cfg.min_alpha}, or use a "
                            f"lighter solid colour instead of transparency.",
                            {"value": val, "func": fname},
                        ))
            elif name in _FG_COLOR_KW:
                found_any = True
                fg_specs.append((kw.arg, val))
            elif name in _BG_COLOR_KW:
                found_any = True
                bg_specs.append((kw.arg, val))

        # Pair foreground vs background colours in the SAME call for contrast.
        for fg_arg, fg_val in fg_specs:
            for bg_arg, bg_val in bg_specs:
                fg_rgb = parse_color(fg_val)
                bg_rgb = parse_color(bg_val)
                if fg_rgb is None or bg_rgb is None:
                    continue
                ratio = contrast_ratio(fg_rgb, bg_rgb)
                if ratio < cfg.min_contrast:
                    issues.append(Issue(
                        "high", "contrast", f"{path.name}:{line}",
                        f"{fname}(..., {fg_arg}={fg_val!r}, {bg_arg}={bg_val!r}) "
                        f"contrast {ratio:.2f}:1 < {cfg.min_contrast}:1 (WCAG-AA).",
                        f"Increase contrast between {fg_arg} and {bg_arg}.",
                        {"ratio": round(ratio, 3),
                         "fg": list(fg_rgb), "bg": list(bg_rgb)},
                    ))

    if not found_any:
        issues.append(Issue(
            "info", "skipped", path.name,
            "No linewidth=/lw=/alpha=/color= kwargs found to evaluate.",
            "", {},
        ))
    return issues


# ── Dispatch + reporting ─────────────────────────────────────────────────────
@dataclass
class Config:
    min_contrast: float
    min_linewidth: float
    min_alpha: float


def detect_mode(path: Path, forced: Optional[str]) -> str:
    if forced and forced != "auto":
        return forced
    ext = path.suffix.lower()
    if ext in CODE_EXTS:
        return "code"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    return "unknown"


def validate_one(path: Path, mode: str, cfg: Config) -> list:
    if mode == "image":
        return validate_image(path, cfg)
    if mode == "code":
        return validate_code(path, cfg)
    if mode == "pdf":
        return validate_pdf(path, cfg)
    return [Issue(
        "error", "error", path.name,
        f"Unknown input type '{path.suffix}'. Supported: images "
        f"({', '.join(sorted(IMAGE_EXTS))}), .py, .pdf. Use --mode to force.",
        "",
    )]


_SEV_LABEL = {
    "error": "ERROR", "high": "HIGH", "medium": "MED", "low": "LOW", "info": "ok",
}


def print_text_report(results: dict, cfg: Config) -> None:
    print("=" * 70)
    print("figure-validate — pre-flight report")
    print(f"thresholds: contrast>={cfg.min_contrast}:1  "
          f"linewidth>={cfg.min_linewidth}pt  alpha>={cfg.min_alpha}")
    print("=" * 70)

    total_findings = 0
    for path, issues in results.items():
        ordered = sorted(issues, key=lambda i: SEV_ORDER.get(i.severity, 9))
        findings = [i for i in ordered if _is_finding(i)]
        total_findings += len(findings)
        status = "FLAG" if findings else "pass"
        print(f"\n[{status}] {path}")
        for i in ordered:
            label = _SEV_LABEL.get(i.severity, i.severity)
            print(f"  ({label:<5}) {i.kind:<10} {i.location}")
            print(f"          {i.message}")
            if i.suggestion:
                print(f"          fix: {i.suggestion}")

    print("\n" + "=" * 70)
    flagged_files = sum(
        1 for iss in results.values()
        if any(_is_finding(i) for i in iss)
    )
    print(f"summary: {flagged_files}/{len(results)} file(s) flagged, "
          f"{total_findings} finding(s) total.")
    print("=" * 70)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="figure-validate.py",
        description="Pre-flight validator for publication figures: WCAG-AA "
                    "contrast, thin lines, over-transparent alpha.",
    )
    p.add_argument("paths", nargs="+", metavar="FIGURE",
                   help="image (.png/.jpg/...), plotting code (.py), or .pdf")
    p.add_argument("--mode", choices=["auto", "image", "code", "pdf"],
                   default="auto", help="force input mode (default: by extension)")
    p.add_argument("--min-contrast", type=float, default=DEFAULT_MIN_CONTRAST,
                   help=f"min WCAG contrast ratio (default {DEFAULT_MIN_CONTRAST})")
    p.add_argument("--min-linewidth", type=float, default=DEFAULT_MIN_LINEWIDTH,
                   help=f"min linewidth in pt (default {DEFAULT_MIN_LINEWIDTH})")
    p.add_argument("--min-alpha", type=float, default=DEFAULT_MIN_ALPHA,
                   help=f"min alpha (default {DEFAULT_MIN_ALPHA})")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of a text report")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = Config(args.min_contrast, args.min_linewidth, args.min_alpha)

    results = {}
    tool_error = False
    for raw in args.paths:
        path = Path(raw).expanduser()
        if not path.exists():
            results[str(path)] = [Issue(
                "error", "error", str(path), "File not found.", "")]
            tool_error = True
            continue
        mode = detect_mode(path, args.mode)
        issues = validate_one(path, mode, cfg)
        results[str(path)] = issues
        if any(i.kind == "error" for i in issues):
            tool_error = True

    if args.json:
        payload = {
            "thresholds": asdict(cfg),
            "results": {
                p: [asdict(i) for i in iss] for p, iss in results.items()
            },
        }
        print(json.dumps(payload, indent=2))
    else:
        print_text_report(results, cfg)

    # Exit non-zero ONLY on the tool's own failure, never on findings.
    return 1 if tool_error else 0


if __name__ == "__main__":
    sys.exit(main())
