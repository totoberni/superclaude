"""PDF artifact rendering + .txt fallback (W4 3.9).

No real pdflatex runs in the default suite: the `fake_pdflatex` fixture (conftest)
stands in for the subprocess. One integration test opts back in and is skipped
when pdflatex is absent.
"""

import shutil

import pytest

from engine.artifacts import (
    _escape_latex,
    _latex_body,
    render_pdf,
    write_txt_fallback,
)

_MATERIAL = "Dear team,\n\nI use 100% Python & C++.\n\nFIELD DATA\nnotice: 1 month"


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
    assert "100" not in body            # sanity: unrelated content absent


def test_render_pdf_success_moves_only_pdf(tmp_path, fake_pdflatex):
    out = tmp_path / "j-1"
    result = render_pdf("j-1", _MATERIAL, out, company_slug="acme",
                        runner=fake_pdflatex())
    assert result is not None
    assert result == out / "j-1-acme-cover-letter.pdf"
    assert result.exists()
    # only the final PDF lands in out_dir; no .tex/.aux/.log leak through
    assert [p.name for p in out.iterdir()] == ["j-1-acme-cover-letter.pdf"]


def test_render_pdf_nonzero_exit_returns_none(tmp_path, fake_pdflatex):
    out = tmp_path / "j-2"
    assert render_pdf("j-2", _MATERIAL, out, runner=fake_pdflatex(False)) is None
    assert not out.exists()  # failed render leaves no directory behind


def test_render_pdf_missing_pdflatex_returns_none(tmp_path):
    def missing(cmd, **kwargs):
        raise FileNotFoundError("pdflatex")

    assert render_pdf("j-3", _MATERIAL, tmp_path / "j-3", runner=missing) is None


def test_render_pdf_no_pdf_produced_returns_none(tmp_path):
    import types

    def rc0_but_no_pdf(cmd, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    out = tmp_path / "j-4"
    assert render_pdf("j-4", _MATERIAL, out, runner=rc0_but_no_pdf) is None


def test_write_txt_fallback_writes_material(tmp_path):
    out = tmp_path / "j-5"
    path = write_txt_fallback("j-5", _MATERIAL, out, company_slug="globex")
    assert path == out / "j-5-globex-cover-letter.txt"
    assert path.read_text() == _MATERIAL


def test_failed_render_then_txt_fallback(tmp_path, fake_pdflatex):
    out = tmp_path / "j-6"
    pdf = render_pdf("j-6", _MATERIAL, out, runner=fake_pdflatex(False))
    assert pdf is None
    txt = write_txt_fallback("j-6", _MATERIAL, out)
    assert txt.exists()
    assert txt.name == "j-6-cover-letter.txt"  # no slug -> no double dash


def test_stem_omits_empty_company_slug(tmp_path, fake_pdflatex):
    out = tmp_path / "j-7"
    result = render_pdf("j-7", _MATERIAL, out, runner=fake_pdflatex())
    assert result.name == "j-7-cover-letter.pdf"


@pytest.mark.skipif(shutil.which("pdflatex") is None,
                    reason="pdflatex not installed (integration only)")
def test_render_pdf_integration_real_pdflatex(tmp_path):
    out = tmp_path / "j-int"
    result = render_pdf("j-int", _MATERIAL, out, company_slug="acme")
    assert result is not None
    assert result.exists()
    assert result.read_bytes().startswith(b"%PDF")
