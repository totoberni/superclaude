"""Test di latex-warn: firme stabili, solo warning nuovi, ricerca del log, ordine hyperref."""
from __future__ import annotations

import os

import pytest

from conftest import msys, win
from sc_hooks import core
from sc_hooks.handlers import latex_warn as lw

BANNER = "This is pdfTeX, Version 3.141592653 (MiKTeX 24.1)\n"
WARN_REF = "LaTeX Warning: Reference `fig:one' on page 3 undefined on input line 42.\n"
WARN_CITE = "LaTeX Warning: Citation `knuth84' on page 1 undefined on input line 7.\n"
WARN_BOX = "Overfull \\hbox (12.3pt too wide) in paragraph at lines 10--12\n"


@pytest.fixture
def proj(home, monkeypatch):
    monkeypatch.setattr(lw, "CACHE_DIR", home / ".claude" / ".latex-warn-cache")
    d = home / "code" / "paper"
    d.mkdir(parents=True)
    (d / "main.tex").write_text("\\documentclass{article}\n\\begin{document}x\\end{document}\n", encoding="utf-8")
    return d


def _ctx(path, tool="Edit", cwd="C:/x"):
    return core.Ctx(event="PostToolUse", payload={}, session_id="s1", tool=tool, tool_input={"file_path": path},
                    cwd=cwd, role="plain", agent="")


def test_non_tex_returns_before_touching_disk(proj, monkeypatch):
    monkeypatch.setattr(lw, "find_log", lambda *_: pytest.fail("non deve cercare log"))
    assert lw.handle(_ctx(win(proj / "notes.md"))) is None
    for ti in ({}, {"file_path": None}, {"file_path": 3}):
        ctx = _ctx("x")
        ctx.tool_input = ti
        assert lw.handle(ctx) is None


def test_no_log_and_non_tex_log_are_silent(proj):
    assert lw.handle(_ctx(win(proj / "main.tex"))) is None
    (proj / "main.log").write_text("npm ERR! something\n" + WARN_REF, encoding="utf-8")
    assert lw.handle(_ctx(win(proj / "main.tex"))) is None
    assert lw.handle(_ctx(win(proj / "missing.tex"))) is None
    assert not lw.CACHE_DIR.exists()


def test_first_sighting_seeds_then_only_new_are_reported(proj):
    log = proj / "main.log"
    log.write_text(BANNER + WARN_REF, encoding="utf-8")
    assert lw.handle(_ctx(win(proj / "main.tex"))) is None                 # semina in silenzio
    # stesso warning con pagina e riga diverse: non è nuovo
    log.write_text(BANNER + WARN_REF.replace("page 3", "page 9").replace("line 42", "line 77"), encoding="utf-8")
    assert lw.handle(_ctx(msys(proj / "main.tex"))) is None
    log.write_text(BANNER + WARN_REF + WARN_CITE + WARN_BOX.replace("12.3pt", "99.9pt"), encoding="utf-8")
    text = lw.handle(_ctx(win(proj / "main.tex"), tool="Write")).context[0]
    assert "2 NEW LaTeX warning(s) since the last build of main.tex (parsed from paper/main.log" in text
    assert "Citation `knuth84' undefined" in text and r"hint: undefined \cite" in text
    assert r"Overfull \hbox (...) in paragraph at lines 10--12" in text and "overfull hbox" in text
    assert "fig:one" not in text
    assert lw.handle(_ctx(win(proj / "main.tex"))) is None                 # cache aggiornata
    # risolto e poi reintrodotto: torna a essere nuovo
    log.write_text(BANNER, encoding="utf-8")
    assert lw.handle(_ctx(win(proj / "main.tex"))) is None
    log.write_text(BANNER + WARN_CITE, encoding="utf-8")
    assert "1 NEW" in lw.handle(_ctx(win(proj / "main.tex"))).context[0]


def test_clean_first_build_then_first_warning_is_new(proj):
    (proj / "main.log").write_text(BANNER, encoding="utf-8")
    assert lw.handle(_ctx("main.tex", cwd=str(proj))) is None             # percorso relativo
    (proj / "main.log").write_text(BANNER + "! LaTeX Error: Too many unprocessed floats.\n", encoding="utf-8")
    text = lw.handle(_ctx("main.tex", cwd=str(proj))).context[0]
    assert "  - Too many unprocessed floats" in text and "FLOAT STACKING" in text


def test_find_log_search_order(proj):
    chapters = proj / "chapters"
    chapters.mkdir()
    tex = chapters / "intro.tex"
    tex.write_text("x", encoding="utf-8")
    assert lw.find_log(tex) is None
    build = proj / "build"
    build.mkdir()
    (build / "main.log").write_text(BANNER)
    (proj / "older.log").write_text(BANNER)
    os.utime(proj / "older.log", (1000, 1000))
    assert lw.find_log(tex) == build / "main.log"                          # entro 2 livelli dal genitore
    (chapters / "a.log").write_text(BANNER)
    (chapters / "b.log").write_text(BANNER)
    os.utime(chapters / "a.log", (2000, 2000))
    assert lw.find_log(tex) == chapters / "b.log"                          # il più recente nella stessa cartella
    (chapters / "intro.log").write_text(BANNER)
    os.utime(chapters / "intro.log", (500, 500))
    assert lw.find_log(tex) == chapters / "intro.log"                      # il gemello vince sempre


def test_log_signatures():
    text = (BANNER + WARN_REF + WARN_REF.replace("page 3", "page 4") + WARN_BOX
            + "Underfull \\vbox (badness 10000) has occurred while \\output is active\n"
            + "LaTeX Warning: Label `sec:a' multiply defined.\n"
            + "LaTeX Font Warning: Font shape `OT1/cmr/bx/sc' undefined\n"
            + "Package hyperref Warning: Option `colorlinks' has already been used,\n"
            + "Package cleveref Error: cleveref must be loaded after hyperref.\n"
            + "Some unrelated line (with parens) at lines 1--2\n")
    assert lw.log_signatures(text) == {
        "LaTeX Warning: Reference `fig:one' undefined",
        r"Overfull \hbox (...) in paragraph at lines 10--12",
        r"Underfull \vbox (...) has occurred while \output is active",
        "LaTeX Warning: Label `sec:a' multiply defined",
        "LaTeX Font Warning: Font shape `OT1/cmr/bx/sc' undefined",
        "Package hyperref Warning: Option `colorlinks' has already been used",
        "Package cleveref Error: cleveref must be loaded after hyperref",
    }


def test_source_order_signatures():
    bad = "\\usepackage{cleveref}\n  \\usepackage[colorlinks]{hyperref}\n" + "".join(
        f"\\usepackage{{pkg{i}}}\n" for i in range(3)) + "\\usepackage{bookmark}\n% \\usepackage{x}\n"
    sigs = lw.source_order_signatures(bad)
    assert "SOURCE hyperref-order: cleveref loaded before hyperref" in sigs
    assert "SOURCE hyperref-order: 3 packages load after hyperref (hyperref should load near-last)" in sigs
    good = "\\usepackage{amsmath}\n\\usepackage{hyperref}\n\\usepackage{cleveref}\n\\usepackage{a}\n"
    assert lw.source_order_signatures(good) == set()
    assert lw.source_order_signatures("\\usepackage{cleveref}\n") == set()


def test_source_order_is_reported_as_new(proj):
    (proj / "main.log").write_text(BANNER, encoding="utf-8")
    lw.handle(_ctx(win(proj / "main.tex")))
    (proj / "main.tex").write_text("\\usepackage{cleveref}\n\\usepackage{hyperref}\n", encoding="utf-8")
    text = lw.handle(_ctx(win(proj / "main.tex"))).context[0]
    assert "cleveref loaded before hyperref" in text and "HYPERREF ORDER - load cleveref AFTER hyperref" in text


@pytest.mark.parametrize("sig,needle", [
    ("LaTeX Warning: Reference `a' undefined", r"undefined \ref"),
    ("LaTeX Warning: Label `a' multiply defined", r"duplicate \label"),
    (r"Underfull \hbox (...) in paragraph at lines 1--2", "underfull hbox"),
    (r"Overfull \vbox (...) has occurred while \output is active", "over/underfull vbox"),
    ("LaTeX Font Warning: Font shape `x' undefined", "font substitution"),
    ("SOURCE hyperref-order: 4 packages load after hyperref (...)", "hyperref should load near-last"),
    ("Package hyperref Warning: Option `x' has already been used", "option conflict"),
    ("something else", "new LaTeX warning"),
])
def test_explain(sig, needle):
    assert needle in lw.explain(sig)


def test_binary_garbage_in_log_does_not_raise(proj):
    (proj / "main.log").write_bytes(BANNER.encode() + bytes(range(256)) * 20 + WARN_REF.encode())
    assert lw.handle(_ctx(win(proj / "main.tex"))) is None
    assert len(list(lw.CACHE_DIR.iterdir())) == 1
