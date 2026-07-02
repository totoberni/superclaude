"""PDF (and .txt fallback) artifact rendering for drafted material (W4 3.9).

The owner reads the tailored cover letters on their phone via ntfy attachments,
so each drafted item's material (cover letter + FIELD DATA block, D2) is rendered
to one PDF through a minimal LaTeX `article` template and `pdflatex`. Model output
is untrusted text: every LaTeX-special character is escaped before it reaches the
compiler, and rendering is FAIL-SOFT. Any failure (pdflatex absent, a non-zero
exit, or no PDF produced) returns None so the runner can fall back to a plain
`.txt` attachment instead of crashing the run.

`pdflatex` runs in a throwaway build directory (`-interaction=nonstopmode
-halt-on-error`); only the finished PDF is moved into `out_dir`, which is created
0700. The subprocess call is injectable (`runner`) so tests drive it with a fake
and never shell out to a real TeX installation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

_PDFLATEX_TIMEOUT_S = 60

# LaTeX-special characters that must never reach the compiler verbatim. A single
# regex pass (re.sub does not re-scan its own replacements) keeps the backslash
# and tilde/caret escapes, which themselves contain braces, from being
# double-escaped.
_LATEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_LATEX_PATTERN = re.compile("|".join(re.escape(ch) for ch in _LATEX_SPECIAL))

_PREAMBLE = (
    r"\documentclass[11pt]{article}",
    r"\usepackage[T1]{fontenc}",
    r"\usepackage[utf8]{inputenc}",
    r"\usepackage[margin=1in]{geometry}",
    r"\usepackage{parskip}",
    r"\begin{document}",
)


def render_pdf(item_id: str, material: str, out_dir: str | Path,
               company_slug: str = "",
               runner: Callable = subprocess.run) -> Path | None:
    """Render `material` to one PDF in `out_dir`, or None on any failure.

    Fail-soft contract: a missing pdflatex, a non-zero exit, or a build that
    produces no PDF all return None; the caller falls back to write_txt_fallback.
    """
    document = _build_document(material)
    stem = _artifact_stem(item_id, company_slug)
    build_dir = Path(tempfile.mkdtemp(prefix="jobhunt-pdf-"))
    try:
        tex_path = build_dir / f"{stem}.tex"
        tex_path.write_text(document, encoding="utf-8")
        try:
            completed = runner(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                 "-output-directory", str(build_dir), tex_path.name],
                cwd=str(build_dir), capture_output=True, text=True,
                timeout=_PDFLATEX_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return None  # pdflatex missing (FileNotFoundError) or timed out
        if getattr(completed, "returncode", 1) != 0:
            return None
        built_pdf = build_dir / f"{stem}.pdf"
        if not built_pdf.exists():
            return None
        final = _ensure_dir(out_dir) / f"{stem}.pdf"
        shutil.move(str(built_pdf), str(final))
        return final
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def write_txt_fallback(item_id: str, material: str, out_dir: str | Path,
                       company_slug: str = "") -> Path:
    """Write `material` verbatim as the .txt fallback attachment (always succeeds)."""
    stem = _artifact_stem(item_id, company_slug)
    path = _ensure_dir(out_dir) / f"{stem}.txt"
    path.write_text(material or "", encoding="utf-8")
    return path


def _build_document(material: str) -> str:
    return "\n".join((*_PREAMBLE, _latex_body(material), r"\end{document}", ""))


def _latex_body(material: str) -> str:
    """Escape the material and preserve its layout: blank lines split paragraphs,
    single newlines become forced line breaks so the FIELD DATA block stays legible."""
    material = (material or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    for block in re.split(r"\n[ \t]*\n+", material.strip()):
        lines = [_escape_latex(line) for line in block.split("\n") if line.strip()]
        if lines:
            paragraphs.append(" \\\\\n".join(lines))
    return "\n\n".join(paragraphs) if paragraphs else _escape_latex("(no material)")


def _escape_latex(text: str) -> str:
    return _LATEX_PATTERN.sub(lambda m: _LATEX_SPECIAL[m.group()], text)


def _artifact_stem(item_id: str, company_slug: str) -> str:
    parts = [_safe(item_id)]
    slug = _safe(company_slug)
    if slug:
        parts.append(slug)
    parts.append("cover-letter")
    return "-".join(p for p in parts if p) or "cover-letter"


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", part or "").strip("-")


def _ensure_dir(out_dir: str | Path) -> Path:
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass  # best-effort private permissions; do not fail the render over this
    return path
