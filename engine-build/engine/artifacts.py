"""PDF (and .txt fallback) artifact rendering for drafted material (W4 3.9 + 4c).

The owner reads each drafted item on their phone via ntfy attachments as TWO
separate documents (W4 4c criterion 4): a cover letter and a report.

- `render_letter_pdf` typesets the LLM letter BODY (salutation + narrative
  paragraphs + sign-off, plain text) under the owner's shared-example layout
  (centered header, right-aligned locale date, recipient block, bold subject,
  criterion 5). Header/recipient/subject are assembled deterministically by the
  caller from the SSOT and posting.
- `render_report_pdf` typesets the structured report: four tabularx tables
  (posting summary, score breakdown, field-data mapping, coverage + warnings +
  [MISSING] pointers). No verbatim dumps.

Model and SSOT text is untrusted: every LaTeX-special character is escaped before
it reaches the compiler, and rendering is FAIL-SOFT. Any failure (pdflatex
absent, a non-zero exit, or no PDF produced) returns None so the runner can fall
back to a plain `.txt` attachment instead of crashing the run.

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
from datetime import date
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

_LETTER_PREAMBLE = (
    r"\documentclass[11pt]{article}",
    r"\usepackage[T1]{fontenc}",
    r"\usepackage[utf8]{inputenc}",
    r"\usepackage[margin=1in]{geometry}",
    r"\usepackage{parskip}",
    r"\pagestyle{empty}",
    r"\begin{document}",
)

_REPORT_PREAMBLE = (
    r"\documentclass[11pt]{article}",
    r"\usepackage[T1]{fontenc}",
    r"\usepackage[utf8]{inputenc}",
    r"\usepackage[margin=1in]{geometry}",
    r"\usepackage{parskip}",
    r"\usepackage{tabularx}",
    r"\usepackage{booktabs}",
    r"\usepackage{url}",
    r"\pagestyle{empty}",
    r"\begin{document}",
)

_MIDDLE_DOT = r" \textperiodcentered{} "

_SUBJECT_PREFIX = {"it": "Oggetto:", "en": "Re:"}
_SALUTATION_HINT = {"it": "Gentili,", "en": "Dear team,"}

_IT_MONTHS = ("", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
              "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre")
_EN_MONTHS = ("", "January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December")

_CONTACT_ORDER = ("email", "phone", "website", "linkedin")


# -- cover letter -------------------------------------------------------------

def render_letter_pdf(item_id: str, letter_text: str, header: dict,
                      recipient: dict, subject: str, lang: str,
                      out_dir: str | Path, runner: Callable = subprocess.run,
                      today: date | None = None) -> Path | None:
    """Render the cover letter to one PDF in `out_dir`, or None on any failure."""
    document = build_letter_document(letter_text, header, recipient, subject,
                                     lang, today=today)
    return _render_tex(document, _artifact_stem(item_id, "cover-letter"),
                       out_dir, runner)


def build_letter_document(letter_text: str, header: dict, recipient: dict,
                          subject: str, lang: str,
                          today: date | None = None) -> str:
    """Assemble the full letter LaTeX per the owner's shared-example layout."""
    # Every text unit is terminated with \par before the following \vspace:
    # \vspace is a no-op in horizontal mode, so without the \par the date,
    # subject line, and salutation collapse onto the recipient's last line.
    body = "\n".join((
        *_LETTER_PREAMBLE,
        _letter_header_block(header),
        r"\noindent\rule{\textwidth}{0.4pt}\par",
        r"\vspace{0.6em}",
        r"{\raggedleft " + _escape_latex(_format_date(today or date.today(), lang))
        + r"\par}",
        r"\vspace{0.6em}",
        _recipient_block(recipient) + r"\par",
        r"\vspace{1em}",
        r"\noindent\textbf{" + _subject_line(subject, lang) + r"}\par",
        r"\vspace{1em}",
        _latex_body(letter_text),
        r"\end{document}",
        "",
    ))
    return body


def _letter_header_block(header: dict) -> str:
    name = _escape_latex(str(header.get("full_name", "")))
    subtitle = _escape_latex(str(header.get("subtitle", "")))
    contact = _contact_line(header)
    return "\n".join((
        r"\begin{center}",
        r"{\Huge\bfseries " + name + r"}\\[4pt]",
        r"{\itshape " + subtitle + r"}\\[3pt]",
        contact,
        r"\end{center}",
    ))


def _contact_line(header: dict) -> str:
    parts = [_escape_latex(str(header[key])) for key in _CONTACT_ORDER
             if header.get(key)]
    return _MIDDLE_DOT.join(parts) if parts else ""


def _recipient_block(recipient: dict) -> str:
    city = str(recipient.get("city", "") or "")
    country = str(recipient.get("country", "") or "")
    place = ", ".join(p for p in (city, country) if p)
    rows = [str(recipient.get("team", "") or ""),
            str(recipient.get("company", "") or ""),
            place]
    escaped = [_escape_latex(r) for r in rows if r]
    return r"\noindent " + (r"\\" + "\n").join(escaped)


def _subject_line(subject: str, lang: str) -> str:
    prefix = _SUBJECT_PREFIX.get(lang, _SUBJECT_PREFIX["en"])
    return f"{prefix} {_escape_latex(str(subject or ''))}"


def _format_date(day: date, lang: str) -> str:
    months = _IT_MONTHS if lang == "it" else _EN_MONTHS
    return f"{day.day} {months[day.month]} {day.year}"


# -- report -------------------------------------------------------------------

def render_report_pdf(item_id: str, report_data: dict, out_dir: str | Path,
                      runner: Callable = subprocess.run) -> Path | None:
    """Render the structured report to one PDF in `out_dir`, or None on failure."""
    document = build_report_document(report_data)
    return _render_tex(document, _artifact_stem(item_id, "report"), out_dir,
                       runner)


def build_report_document(report_data: dict) -> str:
    """Assemble the report LaTeX: four tabularx tables, no verbatim dumps."""
    posting = report_data.get("posting") or {}
    score_rows = report_data.get("score_rows") or []
    field_data = report_data.get("field_data") or []
    coverage = report_data.get("coverage") or {}
    language = report_data.get("language") or {}
    return "\n".join((
        *_REPORT_PREAMBLE,
        _table_posting(posting),
        r"\vspace{1em}",
        _table_scores(score_rows),
        r"\vspace{1em}",
        _table_field_data(field_data),
        r"\vspace{1em}",
        _table_coverage(coverage, language),
        r"\end{document}",
        "",
    ))


def _table_posting(posting: dict) -> str:
    locations = ", ".join(str(x) for x in (posting.get("locations") or [])) or "-"
    url = str(posting.get("url", "") or "")
    rows = [
        ("Vendor", _escape_latex(str(posting.get("vendor", "") or "-"))),
        ("Company", _escape_latex(str(posting.get("company", "") or "-"))),
        ("Title", _escape_latex(str(posting.get("title", "") or "-"))),
        ("Locations", _escape_latex(locations)),
        ("URL", _url(url)),
        ("Score", _escape_latex(str(posting.get("score", "") if
                                    posting.get("score") is not None else "-"))),
    ]
    body = "\n".join(rf"{k} & {v} \\" for k, v in rows)
    return _tabularx("lX", "Table 1: Posting summary", "Field", "Value", body)


def _table_scores(score_rows: list) -> str:
    if score_rows:
        body = "\n".join(
            rf"{_escape_latex(str(r.get('axis', '')))} & "
            rf"{_escape_latex(str(r.get('weight', '')))} & "
            rf"{_escape_latex(str(r.get('subscore', '')))} & "
            rf"{_escape_latex(str(r.get('notes', '')))} \\"
            for r in score_rows)
    else:
        body = r"\multicolumn{4}{l}{no axis breakdown available} \\"
    header = r"Axis & Weight & Subscore & Notes \\"
    return _tabularx_raw("lllX", "Table 2: Score breakdown", header, body)


def _table_field_data(field_data: list) -> str:
    if field_data:
        body = "\n".join(
            rf"{_escape_latex(str(r.get('field', '')))} & "
            rf"{_escape_latex(str(r.get('value', '')))} \\"
            for r in field_data)
    else:
        body = r"\multicolumn{2}{l}{no field data resolved} \\"
    return _tabularx_raw("lX", "Table 3: Field-data mapping",
                         r"ATS field & Value \\", body)


def _table_coverage(coverage: dict, language: dict) -> str:
    warnings = coverage.get("warnings") or []
    missing = coverage.get("missing") or []
    rows = [
        ("Coverage", _escape_latex(str(coverage.get("summary", "") or "-"))),
        ("ATS warnings",
         _escape_latex("; ".join(str(w) for w in warnings) or "none")),
        ("Missing (questionnaire)",
         _escape_latex("; ".join(str(m) for m in missing) or "none")),
        ("Language",
         _escape_latex(f"{language.get('lang', '')} "
                       f"({language.get('rationale', '')})")),
    ]
    body = "\n".join(rf"{k} & {v} \\" for k, v in rows)
    return _tabularx("lX", "Table 4: Coverage, warnings, and gaps",
                     "Item", "Detail", body)


def _tabularx(colspec: str, title: str, head_left: str, head_right: str,
              body: str) -> str:
    header = rf"{head_left} & {head_right} \\"
    return _tabularx_raw(colspec, title, header, body)


def _tabularx_raw(colspec: str, title: str, header_row: str, body: str) -> str:
    # \par after the title (else the caption sits beside the table, not above)
    # and \par\vspace after the environment so successive tables do not collide.
    return "\n".join((
        r"\noindent\textbf{" + _escape_latex(title) + r"}\par",
        r"\vspace{0.3em}",
        r"\begin{tabularx}{\textwidth}{" + colspec + "}",
        r"\toprule",
        header_row,
        r"\midrule",
        body,
        r"\bottomrule",
        r"\end{tabularx}\par",
        r"\vspace{1.2em}",
    ))


# -- shared helpers -----------------------------------------------------------

def write_txt_fallback(item_id: str, material: str, out_dir: str | Path,
                       kind: str = "cover-letter") -> Path:
    """Write `material` verbatim as the .txt fallback attachment (always succeeds).

    `kind` selects the stem suffix ("cover-letter" or "report") so a failed
    letter render and a failed report render never collide on disk.
    """
    stem = _artifact_stem(item_id, kind)
    path = _ensure_dir(out_dir) / f"{stem}.txt"
    path.write_text(material or "", encoding="utf-8")
    return path


def _render_tex(document: str, stem: str, out_dir: str | Path,
                runner: Callable) -> Path | None:
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


def _latex_body(material: str) -> str:
    """Escape the material and preserve its layout: blank lines split paragraphs,
    single newlines become forced line breaks (keeps a salutation and a sign-off
    block on their own lines) while narrative paragraphs stay justified."""
    material = (material or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    for block in re.split(r"\n[ \t]*\n+", material.strip()):
        lines = [_escape_latex(line) for line in block.split("\n") if line.strip()]
        if lines:
            paragraphs.append(" \\\\\n".join(lines))
    return "\n\n".join(paragraphs) if paragraphs else _escape_latex("(no material)")


def _escape_latex(text: str) -> str:
    return _LATEX_PATTERN.sub(lambda m: _LATEX_SPECIAL[m.group()], text)


def _url(url: str) -> str:
    """Wrap a URL in \\url{} (the url package handles its own specials); an empty
    URL degrades to a dash rather than an empty \\url{}."""
    return r"\url{" + url + "}" if url else "-"


def _artifact_stem(item_id: str, kind: str) -> str:
    safe_id = _safe(item_id) or "item"
    return f"{safe_id}-{kind}"


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
