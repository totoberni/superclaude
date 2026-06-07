#!/usr/bin/env python3
"""
render.py — Visual-notation render pipeline for the v3 memory subsystem (T1.4).

Turns a memory's stored ``text`` source into an HTML fragment for the lazy
``get_html`` path in ``memory_db.py``. Five notation kinds are supported:

    render(text, kind) -> html_fragment        kind in {mermaid, dot, vega, tikz, markdown}

Routing:
  * mermaid / dot / vega / markdown  -> CLIENT-SIDE. Emit a container element
    plus the relevant CDN ``<script>`` (mermaid.js, @viz-js/viz, vega-embed,
    marked.js). No Python rendering libraries are used; the browser renders.
  * tikz                              -> SERVER-SIDE. Wrap the snippet in a
    ``standalone`` document with a curated TikZ/quantikz preamble, compile with
    ``latexmk -pdf`` (NO -shell-escape), convert PDF -> SVG with ``dvisvgm``,
    and inline the SVG. The SVG is cached by ``sha256(text)`` under
    ``~/.claude/agent-memory/.render-cache/``. ANY failure degrades gracefully
    to ``<pre>`` + escaped source + a short note -- render() never raises.

CDN-dedup helper:
  Each CDN ``<script>`` should appear once per document. ``render`` is
  self-contained by default (emits the script every call) so a single cached
  fragment is independently viewable. To assemble a whole page without
  duplicate scripts, pass a shared ``ScriptRegistry`` via ``scripts=``: the CDN
  tag is emitted only on first use of each library, plain containers after.

Usage from Python:
    from render import render, ScriptRegistry

    # Lazy per-row (self-contained) -- what memory_db.get_html caches:
    frag = render(memory_text, "mermaid")

    # Whole-page assembly (deduped scripts):
    reg = ScriptRegistry()
    page = "".join(render(t, k, scripts=reg) for t, k in rows)

Usage from CLI (smoke test):
    ~/.claude/.venv/bin/python render.py tikz   < snippet.tex
    ~/.claude/.venv/bin/python render.py mermaid <<< 'graph TD; A-->B;'
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

KINDS = ("mermaid", "dot", "vega", "tikz", "markdown")

CACHE_DIR = Path(os.path.expanduser("~/.claude/agent-memory/.render-cache"))

# Pinned CDN bundles (major-version pinned for reproducibility). Each library
# is requested once per document via ScriptRegistry.
_CDN = {
    "mermaid": "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js",
    "viz": "https://cdn.jsdelivr.net/npm/@viz-js/viz@3/lib/viz-standalone.js",
    # vega-embed bundles vega + vega-lite + vega-embed.
    "vega": "https://cdn.jsdelivr.net/npm/vega-embed@6/build/vega-embed.min.js",
    "marked": "https://cdn.jsdelivr.net/npm/marked@12/marked.min.js",
}

# latexmk per-cell compile budget (seconds). A standalone TikZ figure compiles
# in well under this; the timeout bounds a pathological snippet.
_LATEX_TIMEOUT = 120
_DVISVGM_TIMEOUT = 60

# Curated TikZ preamble (mirrors the T1.4 contract in memory-schema.md).
_TIKZ_PREAMBLE = r"""\documentclass[border=2pt]{standalone}
\usepackage{tikz,pgfplots}
\usetikzlibrary{calc,positioning,arrows.meta,automata}
\usepackage{quantikz}
\pgfplotsset{compat=1.18}"""

# A snippet may declare extra packages with lines like:  % requires: amsmath
_REQUIRES_RE = re.compile(r"^\s*%\s*requires:\s*(.+?)\s*$", re.MULTILINE)

# Strip the XML prolog / DOCTYPE / leading comments so the <svg> drops cleanly
# into an HTML document as an inline element.
_SVG_PROLOG_RE = re.compile(
    r"^\s*(?:<\?xml[^>]*\?>\s*|<!DOCTYPE[^>]*>\s*|<!--.*?-->\s*)+",
    re.DOTALL,
)


# --------------------------------------------------------------------------- #
# CDN-dedup helper
# --------------------------------------------------------------------------- #


class ScriptRegistry:
    """Tracks which CDN ``<script>`` tags have been emitted for one document.

    Pass the same instance to every ``render`` call that builds a single page.
    The first request for a library returns its ``<script>`` tag; subsequent
    requests return ``""`` so the tag is not duplicated.
    """

    def __init__(self) -> None:
        self._emitted: set[str] = set()

    def emit(self, lib: str) -> str:
        """Return the ``<script>`` tag for *lib* the first time only."""
        url = _CDN.get(lib)
        if url is None:
            return ""
        if lib in self._emitted:
            return ""
        self._emitted.add(lib)
        return f'<script src="{html.escape(url, quote=True)}"></script>'

    @property
    def emitted(self) -> "frozenset[str]":
        return frozenset(self._emitted)


def _script_tag(lib: str, scripts: Optional[ScriptRegistry]) -> str:
    """Emit a CDN script tag, deduped through *scripts* if one is supplied.

    With no registry the tag is always emitted (self-contained fragment).
    """
    if scripts is not None:
        return scripts.emit(lib)
    url = _CDN.get(lib, "")
    return f'<script src="{html.escape(url, quote=True)}"></script>' if url else ""


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #


def _frag_id(kind: str, text: str) -> str:
    """Stable, collision-resistant element id for one fragment."""
    return f"mem-{kind}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hidden_source(elem_id: str, text: str) -> str:
    """Stash raw source in a non-rendered node read later via ``.textContent``.

    Using a ``type`` the browser will not execute keeps the source inert; the
    only sequence that must be guarded is a literal ``</script>``. We HTML-escape
    the source -- ``.textContent`` decodes entities back to the original bytes.
    """
    return (
        f'<script type="application/x-memory-src" '
        f'id="{elem_id}-src">{html.escape(text)}</script>'
    )


def _fallback(text: str, note: str) -> str:
    """Graceful degradation: escaped source in a <pre> plus a short caption."""
    return (
        '<figure class="mem-render-fallback">'
        f"<pre>{html.escape(text)}</pre>"
        f'<figcaption class="mem-render-note">{html.escape(note)}</figcaption>'
        "</figure>"
    )


# --------------------------------------------------------------------------- #
# Client-side renderers
# --------------------------------------------------------------------------- #


def _render_mermaid(text: str, scripts: Optional[ScriptRegistry]) -> str:
    # mermaid.js reads the raw diagram text from a <pre class="mermaid"> node's
    # .textContent; HTML entities decode back to source on read.
    tag = _script_tag("mermaid", scripts)
    init = (
        '<script type="module">'
        "import mermaid from "
        "'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';"
        "mermaid.initialize({startOnLoad:true});"
        "</script>"
        if scripts is None or "mermaid-init" not in scripts.emitted
        else ""
    )
    # Mark init as emitted on the registry so a multi-fragment page inits once.
    if scripts is not None and init:
        scripts._emitted.add("mermaid-init")
    body = f'<pre class="mermaid">{html.escape(text)}</pre>'
    # The classic UMD bundle (tag) plus an ESM auto-init both ship from the CDN;
    # we emit the ESM module initializer which is self-sufficient. The UMD tag
    # is kept for environments that block ES modules.
    return f"{tag}{init}{body}"


def _render_dot(text: str, scripts: Optional[ScriptRegistry]) -> str:
    tag = _script_tag("viz", scripts)
    elem = _frag_id("dot", text)
    src = _hidden_source(elem, text)
    runner = (
        f'<div class="mem-graphviz" id="{elem}"></div>'
        f"{src}"
        "<script>(function(){"
        f'var host=document.getElementById("{elem}");'
        f'var src=document.getElementById("{elem}-src").textContent;'
        "function go(){Viz.instance().then(function(v){"
        "host.appendChild(v.renderSVGElement(src));"
        '}).catch(function(e){host.innerHTML='
        '"<pre>"+src.replace(/&/g,"&amp;").replace(/</g,"&lt;")+"</pre>";});}'
        'if(typeof Viz!=="undefined"){go();}'
        "else{var t=setInterval(function(){"
        'if(typeof Viz!=="undefined"){clearInterval(t);go();}},50);}'
        "})();</script>"
    )
    return f"{tag}{runner}"


def _render_vega(text: str, scripts: Optional[ScriptRegistry]) -> str:
    tag = _script_tag("vega", scripts)
    elem = _frag_id("vega", text)
    src = _hidden_source(elem, text)
    runner = (
        f'<div class="mem-vega" id="{elem}"></div>'
        f"{src}"
        "<script>(function(){"
        f'var host=document.getElementById("{elem}");'
        f'var raw=document.getElementById("{elem}-src").textContent;'
        "function go(){try{var spec=JSON.parse(raw);"
        "vegaEmbed(host,spec).catch(function(e){"
        'host.innerHTML="<pre>"+raw.replace(/&/g,"&amp;")'
        '.replace(/</g,"&lt;")+"</pre>";});}'
        "catch(e){host.innerHTML="
        '"<pre>"+raw.replace(/&/g,"&amp;").replace(/</g,"&lt;")+"</pre>";}}'
        'if(typeof vegaEmbed!=="undefined"){go();}'
        "else{var t=setInterval(function(){"
        'if(typeof vegaEmbed!=="undefined"){clearInterval(t);go();}},50);}'
        "})();</script>"
    )
    return f"{tag}{runner}"


def _render_markdown(text: str, scripts: Optional[ScriptRegistry]) -> str:
    tag = _script_tag("marked", scripts)
    elem = _frag_id("markdown", text)
    src = _hidden_source(elem, text)
    runner = (
        f'<div class="mem-markdown" id="{elem}"></div>'
        f"{src}"
        "<script>(function(){"
        f'var host=document.getElementById("{elem}");'
        f'var raw=document.getElementById("{elem}-src").textContent;'
        "function go(){host.innerHTML=marked.parse(raw);}"
        'if(typeof marked!=="undefined"){go();}'
        "else{var t=setInterval(function(){"
        'if(typeof marked!=="undefined"){clearInterval(t);go();}},50);}'
        "})();</script>"
    )
    return f"{tag}{runner}"


# --------------------------------------------------------------------------- #
# Server-side TikZ renderer
# --------------------------------------------------------------------------- #


def _extra_packages(text: str) -> str:
    """Build ``\\usepackage`` lines from ``% requires: <pkg>`` declarations."""
    pkgs: list[str] = []
    for m in _REQUIRES_RE.finditer(text):
        for token in re.split(r"[,\s]+", m.group(1)):
            token = token.strip()
            # Conservative whitelist on the package name to keep an injected
            # `% requires:` line from smuggling arbitrary LaTeX into the preamble.
            if token and re.fullmatch(r"[A-Za-z0-9._-]+", token):
                pkgs.append(rf"\usepackage{{{token}}}")
    return "\n".join(pkgs)


def _build_document(text: str) -> str:
    extra = _extra_packages(text)
    preamble = _TIKZ_PREAMBLE + (("\n" + extra) if extra else "")
    return f"{preamble}\n\\begin{{document}}\n{text}\n\\end{{document}}\n"


def _inline_svg(svg: str) -> str:
    """Strip XML prolog/DOCTYPE so the <svg> embeds cleanly inline."""
    return _SVG_PROLOG_RE.sub("", svg, count=1).strip()


def _compile_tikz(text: str) -> tuple[Optional[str], str]:
    """Compile a TikZ snippet to an inline SVG string.

    Returns ``(svg_or_None, note)``. ``note`` describes a failure when svg is
    None. Never raises -- all failure modes are caught and reported.
    """
    if not shutil.which("latexmk"):
        return None, "latexmk not found"
    if not shutil.which("dvisvgm"):
        return None, "dvisvgm not found"

    tmp = tempfile.mkdtemp(prefix="mem-tikz-")
    try:
        tex_path = Path(tmp) / "fig.tex"
        tex_path.write_text(_build_document(text), encoding="utf-8")

        # NO -shell-escape (untrusted source); fail fast on the first error.
        try:
            proc = subprocess.run(
                [
                    "latexmk",
                    "-pdf",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-no-shell-escape",
                    "fig.tex",
                ],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=_LATEX_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return None, f"latexmk timed out after {_LATEX_TIMEOUT}s"

        pdf_path = Path(tmp) / "fig.pdf"
        if proc.returncode != 0 or not pdf_path.exists():
            reason = _first_tex_error(proc.stdout) or "latexmk compile failed"
            return None, reason

        svg_path = Path(tmp) / "fig.svg"
        try:
            dv = subprocess.run(
                ["dvisvgm", "--pdf", "--no-fonts", "-o", "fig.svg", "fig.pdf"],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=_DVISVGM_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return None, f"dvisvgm timed out after {_DVISVGM_TIMEOUT}s"

        if dv.returncode != 0 or not svg_path.exists():
            return None, "dvisvgm PDF->SVG conversion failed"

        return _inline_svg(svg_path.read_text(encoding="utf-8")), ""
    except Exception as exc:  # never let the page break
        return None, f"tikz render error: {type(exc).__name__}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _first_tex_error(log: str) -> str:
    """Extract the first ``! ...`` TeX error line for a concise fallback note."""
    for line in (log or "").splitlines():
        if line.startswith("!"):
            return "LaTeX: " + line.lstrip("! ").strip()[:160]
    return ""


def _render_tikz(text: str) -> str:
    """Render TikZ to inline SVG, caching by sha256(text). Never raises."""
    key = _text_hash(text)
    cache_file = CACHE_DIR / f"{key}.svg"

    # Cache hit -> wrap and return.
    try:
        if cache_file.is_file():
            return _wrap_svg(cache_file.read_text(encoding="utf-8"))
    except OSError:
        pass  # unreadable cache entry -> fall through and recompile

    svg, note = _compile_tikz(text)
    if svg is None:
        return _fallback(text, note or "TikZ render unavailable")

    # Best-effort cache write (a write failure must not break rendering).
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".svg.tmp")
        tmp.write_text(svg, encoding="utf-8")
        os.replace(tmp, cache_file)
    except OSError:
        pass

    return _wrap_svg(svg)


def _wrap_svg(svg: str) -> str:
    return f'<figure class="mem-tikz">{svg}</figure>'


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def render(text: str, kind: str, scripts: Optional[ScriptRegistry] = None) -> str:
    """Render *text* of notation *kind* to a self-contained HTML fragment.

    Parameters
    ----------
    text : str
        The raw notation source as stored in ``memories.text``.
    kind : str
        One of ``{mermaid, dot, vega, tikz, markdown}``.
    scripts : ScriptRegistry, optional
        When assembling a whole page, pass a shared registry so each CDN
        ``<script>`` is emitted once. Omit for a self-contained fragment
        (the lazy ``get_html`` per-row cache path).

    Returns
    -------
    str
        An HTML fragment. Never raises: an unknown kind or a server-side
        failure degrades to ``<pre>`` + escaped source + a short note.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)

    if kind == "tikz":
        return _render_tikz(text)
    if kind == "mermaid":
        return _render_mermaid(text, scripts)
    if kind == "dot":
        return _render_dot(text, scripts)
    if kind == "vega":
        return _render_vega(text, scripts)
    if kind == "markdown":
        return _render_markdown(text, scripts)

    return _fallback(text, f"unknown render kind: {kind!r}")


# --------------------------------------------------------------------------- #
# CLI smoke test
# --------------------------------------------------------------------------- #


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in KINDS:
        sys.stderr.write(
            f"usage: {argv[0]} {{{'|'.join(KINDS)}}} < source\n"
        )
        return 2
    kind = argv[1]
    source = sys.stdin.read()
    sys.stdout.write(render(source, kind))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
