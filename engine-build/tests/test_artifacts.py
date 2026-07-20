"""Cover-letter + report PDF rendering and .txt fallbacks (W4 3.9 + 4c).

No real pdflatex runs in the default suite: the `fake_pdflatex` fixture (conftest)
stands in for the subprocess. Two integration tests opt back in and are skipped
when pdflatex is absent. The decode-policy tests at the bottom do start a real
local process (a throwaway script standing in for the pdflatex binary), because
decoding is precisely what an in-process fake cannot exercise.
"""

import logging
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.artifacts import (
    _escape_latex,
    _latex_body,
    build_letter_document,
    build_report_document,
    render_letter_pdf,
    render_report_pdf,
    write_txt_fallback,
)

_LETTER_BODY = (
    "Dear Acme team,\n\n"
    "Your line about owning data services in Python is exactly why I use 100% "
    "of my energy on backend work.\n\n"
    "Best regards,\nTest Candidate"
)
_HEADER = {
    "full_name": "Test Candidate",
    "subtitle": "Computational Scientist",
    "email": "test.candidate@example.invalid",
    "phone": "[MISSING: identity.phone]",
    "website": "https://example.invalid",
    "linkedin": "[MISSING: links.linkedin]",
}
_RECIPIENT = {"team": "Hiring Team", "company": "Acme", "city": "London",
              "country": "UK"}
_RECIPIENT_IT = {"team": "Hiring Team", "company": "Acme", "city": "Milano",
                 "country": "Italia"}

_REPORT_DATA = {
    "posting": {"vendor": "greenhouse", "company": "Acme",
                "title": "Senior Backend Engineer",
                "locations": ["London, UK"], "url": "https://x.invalid/j/1",
                "score": 85},
    "score_rows": [
        {"axis": "role_fit", "weight": "0.30", "subscore": "-",
         "notes": "role: Senior Backend Engineer"},
        {"axis": "comp_fit", "weight": "0.10", "subscore": "-",
         "notes": "weak: comp unknown"},
    ],
    "field_data": [
        {"field": "Email", "value": "test.candidate@example.invalid"},
        {"field": "canned: notice_period", "value": "1 month"},
    ],
    "coverage": {"summary": "no field map captured for this posting",
                 "warnings": ["may fail ATS: missing work_authorization"],
                 "missing": []},
    "language": {"lang": "en", "rationale": "description not detected as Italian"},
}


# -- escaping -----------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("100%", r"100\%"),
    ("a & b", r"a \& b"),
    ("cost $5", r"cost \$5"),
    ("tag #1", r"tag \#1"),
    ("snake_case", r"snake\_case"),
    ("a{b}c", r"a\{b\}c"),
    ("~home", r"\textasciitilde{}home"),
    ("x^2", r"x\textasciicircum{}2"),
    (r"a\b", r"a\textbackslash{}b"),
])
def test_escape_latex_special_characters(raw, expected):
    assert _escape_latex(raw) == expected


def test_escape_latex_does_not_double_escape_backslash():
    # The braces introduced by \textbackslash{} must NOT themselves be escaped.
    assert _escape_latex("\\") == r"\textbackslash{}"


def test_latex_body_splits_paragraphs_and_keeps_lines():
    body = _latex_body("line one\nline two\n\nsecond para")
    assert r"line one \\" in body       # single newline -> forced line break
    assert "\n\nsecond para" in body    # blank line -> new paragraph


# -- cover-letter document ----------------------------------------------------

def test_letter_document_english_tokens_and_layout():
    doc = build_letter_document(_LETTER_BODY, _HEADER, _RECIPIENT,
                                "Senior Backend Engineer", "en",
                                today=date(2026, 7, 1))
    # centered header: name (Huge bold), italic subtitle, middle-dot contact line
    assert r"{\Huge\bfseries Test Candidate}" in doc
    assert r"{\itshape Computational Scientist}" in doc
    assert r"\textperiodcentered{}" in doc
    assert "test.candidate@example.invalid" in doc
    # full-width rule + right-aligned locale date
    assert r"\rule{\textwidth}{0.4pt}" in doc
    assert "1 July 2026" in doc
    # recipient block + bold English subject line
    assert "Hiring Team" in doc
    assert r"\textbf{Re: Senior Backend Engineer}" in doc
    # salutation + sign-off come from the letter body
    assert "Dear Acme team," in doc
    assert "Best regards," in doc
    # a [MISSING: ...] header field survives to the letter (grounding contract)
    assert "[MISSING: identity.phone]" in doc
    # no page numbers
    assert r"\pagestyle{empty}" in doc


def test_letter_document_italian_tokens():
    doc = build_letter_document(
        "Gentili,\n\nIl vostro lavoro mi convince.\n\nCordiali saluti,\n"
        "Test Candidate",
        _HEADER, _RECIPIENT_IT, "Ingegnere Backend", "it",
        today=date(2026, 6, 25))
    assert r"\textbf{Oggetto: Ingegnere Backend}" in doc
    assert "Gentili," in doc
    assert "25 giugno 2026" in doc


def test_letter_document_escapes_specials_in_body():
    doc = build_letter_document("Dear team,\n\nI use 100% Python & C++.",
                                _HEADER, _RECIPIENT, "Role", "en",
                                today=date(2026, 7, 1))
    assert r"100\% Python \& C++" in doc
    assert "100%" not in doc.replace(r"100\%", "")  # raw percent never leaks


# -- report document ----------------------------------------------------------

def test_report_document_has_four_tabularx_tables():
    doc = build_report_document(_REPORT_DATA)
    assert doc.count(r"\begin{tabularx}") == 4
    assert doc.count(r"\end{tabularx}") == 4
    # booktabs rules, one set per table
    assert doc.count(r"\toprule") == 4
    assert doc.count(r"\bottomrule") == 4


def test_report_document_uses_tabularx_and_no_verbatim_dumps():
    doc = build_report_document(_REPORT_DATA)
    assert r"\usepackage{tabularx}" in doc
    assert r"\usepackage{booktabs}" in doc
    assert r"\begin{verbatim}" not in doc
    assert r"\verb" not in doc


def test_report_document_table_headers_and_url_wrapping():
    doc = build_report_document(_REPORT_DATA)
    assert "Table 1: Posting summary" in doc
    assert "Table 2: Score breakdown" in doc
    assert "Table 3: Field-data mapping" in doc
    assert "Table 4: Coverage" in doc
    assert r"\url{https://x.invalid/j/1}" in doc
    # column specs: lX (label/value) and lllX (axis/weight/subscore/notes)
    assert r"\begin{tabularx}{\textwidth}{lX}" in doc
    assert r"\begin{tabularx}{\textwidth}{lllX}" in doc


def test_report_document_survives_empty_sections():
    doc = build_report_document({})
    assert doc.count(r"\begin{tabularx}") == 4  # placeholders, still four tables
    assert "no field data resolved" in doc
    assert "no axis breakdown available" in doc


# -- PDF rendering (faked pdflatex) -------------------------------------------

def test_render_letter_pdf_success_moves_only_pdf(tmp_path, fake_pdflatex):
    out = tmp_path / "j-1"
    result = render_letter_pdf("j-1", _LETTER_BODY, _HEADER, _RECIPIENT,
                               "Senior Backend Engineer", "en", out,
                               runner=fake_pdflatex())
    assert result == out / "j-1-cover-letter.pdf"
    assert result.exists()
    assert [p.name for p in out.iterdir()] == ["j-1-cover-letter.pdf"]


def test_render_letter_pdf_nonzero_exit_returns_none(tmp_path, fake_pdflatex):
    out = tmp_path / "j-2"
    assert render_letter_pdf("j-2", _LETTER_BODY, _HEADER, _RECIPIENT, "Role",
                             "en", out, runner=fake_pdflatex(False)) is None
    assert not out.exists()


def test_render_letter_pdf_missing_pdflatex_returns_none(tmp_path):
    def missing(cmd, **kwargs):
        raise FileNotFoundError("pdflatex")

    assert render_letter_pdf("j-3", _LETTER_BODY, _HEADER, _RECIPIENT, "Role",
                             "en", tmp_path / "j-3", runner=missing) is None


def test_render_report_pdf_success(tmp_path, fake_pdflatex):
    out = tmp_path / "j-4"
    result = render_report_pdf("j-4", _REPORT_DATA, out, runner=fake_pdflatex())
    assert result == out / "j-4-report.pdf"
    assert result.exists()
    assert [p.name for p in out.iterdir()] == ["j-4-report.pdf"]


def test_render_report_pdf_failure_returns_none(tmp_path, fake_pdflatex):
    out = tmp_path / "j-5"
    assert render_report_pdf("j-5", _REPORT_DATA, out,
                             runner=fake_pdflatex(False)) is None


# -- txt fallbacks ------------------------------------------------------------

def test_write_txt_fallback_letter_and_report_kinds(tmp_path):
    out = tmp_path / "j-6"
    letter = write_txt_fallback("j-6", "letter body", out, kind="cover-letter")
    report = write_txt_fallback("j-6", "report text", out, kind="report")
    assert letter.name == "j-6-cover-letter.txt"
    assert report.name == "j-6-report.txt"
    assert letter.read_text() == "letter body"
    assert report.read_text() == "report text"


def test_failed_letter_render_then_txt_fallback(tmp_path, fake_pdflatex):
    out = tmp_path / "j-7"
    pdf = render_letter_pdf("j-7", _LETTER_BODY, _HEADER, _RECIPIENT, "Role",
                            "en", out, runner=fake_pdflatex(False))
    assert pdf is None
    txt = write_txt_fallback("j-7", _LETTER_BODY, out, kind="cover-letter")
    assert txt.exists()
    assert txt.name == "j-7-cover-letter.txt"


# -- integration (real pdflatex) ----------------------------------------------

@pytest.mark.skipif(shutil.which("pdflatex") is None,
                    reason="pdflatex not installed (integration only)")
def test_render_letter_pdf_integration_real_pdflatex(tmp_path):
    out = tmp_path / "j-int"
    result = render_letter_pdf("j-int", _LETTER_BODY, _HEADER, _RECIPIENT,
                               "Senior Backend Engineer", "en", out,
                               today=date(2026, 7, 1))
    assert result is not None
    assert result.read_bytes().startswith(b"%PDF")


@pytest.mark.skipif(shutil.which("pdflatex") is None,
                    reason="pdflatex not installed (integration only)")
def test_render_report_pdf_integration_real_pdflatex(tmp_path):
    out = tmp_path / "r-int"
    result = render_report_pdf("r-int", _REPORT_DATA, out)
    assert result is not None
    assert result.read_bytes().startswith(b"%PDF")


# --------------------------------------------------------------------------- #
# DECODE POLICY: pdflatex is a foreign program whose log is NOT reliably UTF-8
# (it echoes source content and emits its own messages as raw bytes). Under a
# strict decode ONE such byte raised UnicodeDecodeError out of subprocess, past
# the (OSError, SubprocessError) handler in _render_tex, and killed the daily run
# at this exact site (production, 2026-07-20). These tests pin the non-strict
# policy, prove it does not mask a genuine LaTeX failure, and pin the signal.
# --------------------------------------------------------------------------- #

# A realistic pdflatex log carrying one byte that is not valid UTF-8.
_BAD_TEX_LOG = (b"This is pdfTeX, Version 3.141592653-2.6-1.40.25\n"
                b"LaTeX Font Warning: undefined shape for \x88 in this document\n"
                b"Output written on doc.pdf (1 page, 42 bytes).\n")
_CLEAN_TEX_LOG = b"This is pdfTeX, Version 3.141592653-2.6-1.40.25\nDone.\n"


def _fake_pdflatex_bin(tmp_path, payload: bytes, create_pdf: bool = True,
                       returncode: int = 0,
                       name: str = "fake_pdflatex") -> str:
    """An executable stand-in for the pdflatex binary that writes `payload` to
    stdout as RAW bytes, so the REAL subprocess.run performs the decode. It
    honours -output-directory the way pdflatex does, so a successful run leaves
    a stub PDF exactly where _render_tex looks for it."""
    script = tmp_path / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.stdout.buffer.write({payload!r})\n"
        f"if {create_pdf!r}:\n"
        "    out = sys.argv[sys.argv.index('-output-directory') + 1]\n"
        "    stem = Path(sys.argv[-1]).stem\n"
        "    (Path(out) / (stem + '.pdf')).write_bytes(b'%PDF-1.4 stub\\n%%EOF\\n')\n"
        f"sys.exit({returncode})\n"
    )
    script.chmod(0o755)
    return str(script)


def _real_runner(fake_bin: str):
    """Forward _render_tex's OWN kwargs to a real subprocess, swapping only the
    binary. The production decode kwargs are therefore the ones under test."""
    def runner(cmd, **kwargs):
        return subprocess.run([fake_bin, *cmd[1:]], **kwargs)
    return runner


def _render_letter(out, runner):
    return render_letter_pdf("j-dec", _LETTER_BODY, _HEADER, _RECIPIENT, "Role",
                             "en", out, runner=runner)


def test_strict_decode_of_pdflatex_output_would_raise(tmp_path):
    """Teeth for the regressions below: the kwargs _render_tex used to pass DO
    raise on this exact TeX log, so the passing tests that follow are not
    vacuous."""
    cmd = [_fake_pdflatex_bin(tmp_path, _BAD_TEX_LOG, create_pdf=False)]
    with pytest.raises(UnicodeDecodeError):
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def test_render_pdf_survives_undecodable_pdflatex_output(tmp_path):
    """The production crash, end to end: a TeX log with an undecodable byte now
    renders normally instead of aborting the run."""
    out = tmp_path / "j-ok"
    result = _render_letter(out, _real_runner(
        _fake_pdflatex_bin(tmp_path, _BAD_TEX_LOG)))
    assert result == out / "j-dec-cover-letter.pdf"
    assert result.read_bytes().startswith(b"%PDF")


def test_undecodable_output_does_not_mask_a_genuine_pdflatex_failure(tmp_path):
    """Not raising must not become not failing: a real LaTeX failure still
    returns None, and the EXIT STATUS is what decides that, not the presence of
    a PDF or the decodability of the log."""
    out = tmp_path / "j-fail"
    assert _render_letter(out, _real_runner(_fake_pdflatex_bin(
        tmp_path, _BAD_TEX_LOG, create_pdf=False, returncode=1))) is None
    assert not out.exists()
    # rc 1 that nonetheless left a PDF behind is still a failure
    assert _render_letter(out, _real_runner(_fake_pdflatex_bin(
        tmp_path, _BAD_TEX_LOG, create_pdf=True, returncode=1,
        name="fake_pdflatex_rc1_pdf"))) is None
    assert not out.exists()


def test_undecodable_pdflatex_output_is_recorded_on_failure(tmp_path, caplog):
    """A lossy log is attributable when it is the diagnosis (failed render), and
    is DEBUG rather than WARNING when the PDF came out fine, so a routine
    substitution does not train the operator to ignore warnings."""
    with caplog.at_level(logging.DEBUG, logger="engine.artifacts"):
        assert _render_letter(tmp_path / "j-w", _real_runner(
            _fake_pdflatex_bin(tmp_path, _BAD_TEX_LOG, create_pdf=False,
                               returncode=1))) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "1 undecodable byte(s)" in warnings[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="engine.artifacts"):
        assert _render_letter(tmp_path / "j-d", _real_runner(
            _fake_pdflatex_bin(tmp_path, _BAD_TEX_LOG,
                               name="fake_pdflatex_ok"))) is not None
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
    assert any("undecodable" in r.getMessage() for r in caplog.records)


def test_clean_pdflatex_output_records_nothing(tmp_path, caplog):
    """The signal discriminates rather than always firing."""
    with caplog.at_level(logging.DEBUG, logger="engine.artifacts"):
        assert _render_letter(tmp_path / "j-c", _real_runner(
            _fake_pdflatex_bin(tmp_path, _CLEAN_TEX_LOG))) is not None
    assert [r for r in caplog.records if "undecodable" in r.getMessage()] == []


def test_render_tex_passes_non_strict_decode_to_runner(tmp_path):
    """The injected-runner contract still gets text mode, but never the strict
    default: this kwarg pair is what stops the crash from coming back."""
    seen: dict = {}

    def runner(cmd, **kwargs):
        seen.update(kwargs)
        build_dir = Path(kwargs["cwd"])
        (build_dir / f"{Path(cmd[-1]).stem}.pdf").write_bytes(b"%PDF-1.4 stub\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert render_report_pdf("j-kw", _REPORT_DATA, tmp_path / "j-kw",
                             runner=runner) is not None
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"
    assert "text" not in seen
