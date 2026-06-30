#!/usr/bin/env python3
"""render_plan.py <abs-path-to-plan.md>

Renders a superclaude plan.md to plan.html in the same directory.
Converts markdown via python-markdown (tables + fenced_code extensions),
renders ```mermaid blocks as <pre class="mermaid"> with Mermaid.js CDN,
inlines full CSS (max-width ~1100px, system font, zebra tables, header band,
sticky TOC, amber gate rows, status badges).

Idempotent — overwrites any existing plan.html.
Prints: output path, table count, mermaid count.
"""

import re
import sys
import html
from pathlib import Path
import markdown
from markdown.preprocessors import Preprocessor
from markdown.extensions import Extension


# ── Mermaid pre-extraction extension ─────────────────────────────────────────
# We intercept ```mermaid blocks BEFORE python-markdown processes fenced_code,
# replacing them with placeholder tokens, then re-insert them as
# <pre class="mermaid">...</pre> in postprocessing.

_MERMAID_PLACEHOLDER_RE = re.compile(r"MERMAID_PLACEHOLDER_(\d+)")


class MermaidPreprocessor(Preprocessor):
    def __init__(self, md, mermaid_blocks: list):
        super().__init__(md)
        self.mermaid_blocks = mermaid_blocks

    def run(self, lines):
        new_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if re.match(r"^```mermaid\s*$", line.strip()) or line.strip() == "```mermaid":
                # collect until closing ```
                block_lines = []
                i += 1
                while i < len(lines) and lines[i].strip() != "```":
                    block_lines.append(lines[i])
                    i += 1
                idx = len(self.mermaid_blocks)
                self.mermaid_blocks.append("\n".join(block_lines))
                # Emit a blank-line-wrapped paragraph so markdown doesn't swallow it
                new_lines.append("")
                new_lines.append(f"MERMAID_PLACEHOLDER_{idx}")
                new_lines.append("")
                i += 1  # skip closing ```
            else:
                new_lines.append(line)
                i += 1
        return new_lines


class MermaidExtension(Extension):
    def __init__(self, mermaid_blocks: list):
        self.mermaid_blocks = mermaid_blocks
        super().__init__()

    def extendMarkdown(self, md):
        md.preprocessors.register(
            MermaidPreprocessor(md, self.mermaid_blocks), "mermaid_pre", 175
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert a heading text to an HTML anchor slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = text.strip("-")
    return text


def extract_meta(lines: list[str]) -> dict:
    """Pull campaign, status, owner, date from the top metadata table."""
    meta = {"campaign": "", "status": "", "owner": "", "date": ""}
    for line in lines[:20]:
        if "**Campaign**" in line or "Campaign" in line:
            m = re.search(r"\|\s*\*\*Campaign\*\*\s*\|\s*(.+?)\s*\|", line)
            if m:
                meta["campaign"] = m.group(1).strip()
        if "**Status**" in line or "Status" in line:
            m = re.search(r"\|\s*\*\*Status\*\*\s*\|\s*(.+?)\s*\|", line)
            if m:
                meta["status"] = re.sub(r"\*\*|\*", "", m.group(1)).strip()
        if "**Owner**" in line or "Owner" in line:
            m = re.search(r"\|\s*\*\*Owner\*\*\s*\|\s*(.+?)\s*\|", line)
            if m:
                meta["owner"] = m.group(1).strip()
        if "**Date**" in line or "Date" in line:
            m = re.search(r"\|\s*\*\*Date\*\*\s*\|\s*(.+?)\s*\|", line)
            if m:
                meta["date"] = m.group(1).strip()
    return meta


def extract_headings(lines: list[str]) -> list[tuple[int, str, str]]:
    """Return list of (level, text, slug) for h1/h2/h3 headings."""
    headings = []
    for line in lines:
        m = re.match(r"^(#{1,3})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            slug = slugify(text)
            headings.append((level, text, slug))
    return headings


def build_toc(headings: list[tuple[int, str, str]]) -> str:
    """Build sticky TOC HTML from headings list."""
    items = []
    for level, text, slug in headings:
        cls = "sub" if level > 1 else ""
        items.append(
            f'      <li class="{cls}"><a href="#{slug}">{html.escape(text)}</a></li>'
            if cls
            else f'      <li><a href="#{slug}">{html.escape(text)}</a></li>'
        )
    return "\n".join(items)


def inject_anchors(body_html: str, headings: list[tuple[int, str, str]]) -> str:
    """Inject id attributes into heading tags so TOC links work."""
    for level, text, slug in headings:
        tag = f"h{level}"
        # Match the opening tag + text (escaped or raw) — use a simple find/replace
        escaped_text = html.escape(text)
        # Try escaped first, then raw
        for candidate in (escaped_text, text):
            old = f"<{tag}>{candidate}</{tag}>"
            new = f'<{tag} id="{slug}">{candidate}</{tag}>'
            if old in body_html:
                body_html = body_html.replace(old, new, 1)
                break
    return body_html


def badge(word: str) -> str:
    """Return a <span class="badge ..."> for a status word."""
    w = word.upper()
    if w == "CONFIRMED":
        return f'<span class="badge badge-confirmed">{word}</span>'
    if w in ("DRAFT", "OPEN"):
        cls = "badge-draft" if w == "DRAFT" else "badge-open"
        return f'<span class="badge {cls}">{word}</span>'
    return word


def apply_badges(body_html: str) -> str:
    """Wrap CONFIRMED/DRAFT/OPEN words in badge spans."""
    def replacer(m):
        return badge(m.group(0))
    return re.sub(r"\b(CONFIRMED|DRAFT|OPEN)\b", replacer, body_html)


def apply_gate_markers(body_html: str) -> str:
    """Wrap 🚪 in a span so CSS :has() selector can highlight the row."""
    return body_html.replace("🚪", '<span class="gate-marker">🚪</span>')


def insert_mermaid(body_html: str, mermaid_blocks: list[str]) -> str:
    """Replace MERMAID_PLACEHOLDER_N paragraphs with mermaid divs."""
    def replacer(m):
        idx = int(m.group(1))
        content = mermaid_blocks[idx]
        escaped = html.escape(content)
        return (
            f'<div class="diagram-wrap">'
            f'<pre class="mermaid">{escaped}</pre>'
            f"</div>"
        )

    # python-markdown wraps the placeholder in <p>...</p>
    body_html = re.sub(
        r"<p>MERMAID_PLACEHOLDER_(\d+)</p>", replacer, body_html
    )
    # fallback: bare placeholder (no <p> wrap)
    body_html = re.sub(r"MERMAID_PLACEHOLDER_(\d+)", replacer, body_html)
    return body_html


CSS = """\
  /* ── Reset + base ── */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html { font-size: 16px; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: #f7f8fc;
    color: #1a1a2e;
    line-height: 1.65;
    padding: 0;
  }

  /* ── Header band ── */
  .site-header {
    background: linear-gradient(135deg, #1a237e 0%, #283593 60%, #3949ab 100%);
    color: #fff;
    padding: 2rem 2.5rem 1.5rem;
    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: 0 2px 8px rgba(0,0,0,.25);
    display: flex;
    align-items: flex-start;
    gap: 2rem;
  }
  .site-header-text { flex: 1; }
  .site-header h1 { font-size: 1.55rem; font-weight: 700; letter-spacing: -.5px; margin-bottom: .3rem; }
  .site-header .meta { font-size: .82rem; opacity: .8; display: flex; gap: 1.2rem; flex-wrap: wrap; }
  .site-header .meta span { display: flex; align-items: center; gap: .3rem; }

  /* ── Layout ── */
  .layout { display: flex; max-width: 1300px; margin: 0 auto; padding: 0; }

  /* ── Sticky TOC ── */
  .toc {
    position: sticky;
    top: 100px;
    width: 230px;
    min-width: 230px;
    max-height: calc(100vh - 120px);
    overflow-y: auto;
    align-self: flex-start;
    padding: 1.2rem 1rem 1.2rem 1.4rem;
    background: #fff;
    border-right: 1px solid #e0e4ef;
    font-size: .8rem;
  }
  .toc h2 { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em; color: #5c6bc0; margin-bottom: .6rem; font-weight: 700; }
  .toc ul { list-style: none; }
  .toc ul li { margin: .22rem 0; }
  .toc ul li a { color: #3949ab; text-decoration: none; padding: .1rem .3rem; border-radius: 3px; display: block; transition: background .15s; }
  .toc ul li a:hover { background: #e8eaf6; }
  .toc ul li.sub { padding-left: .9rem; }
  .toc ul li.sub a { color: #546e7a; font-size: .77rem; }

  /* ── Main content ── */
  .main {
    flex: 1;
    padding: 2.5rem 2.8rem 4rem;
    min-width: 0;
  }

  /* ── Typography ── */
  h1 { font-size: 2rem; margin: 2rem 0 .6rem; color: #1a237e; border-bottom: 2px solid #c5cae9; padding-bottom: .4rem; }
  h2 { font-size: 1.45rem; margin: 2.2rem 0 .7rem; color: #283593; border-bottom: 1px solid #dde; padding-bottom: .3rem; }
  h3 { font-size: 1.15rem; margin: 1.8rem 0 .5rem; color: #3949ab; }
  h4 { font-size: 1rem; margin: 1.4rem 0 .4rem; color: #4a6fa5; }
  p { margin: .7rem 0; }
  ul, ol { margin: .5rem 0 .5rem 1.6rem; }
  li { margin: .2rem 0; }
  a { color: #3949ab; }
  code { font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size: .85em; background: #eef0f8; padding: .15em .35em; border-radius: 3px; }
  pre { background: #1e2240; color: #cdd6f4; padding: 1rem 1.2rem; border-radius: 6px; overflow-x: auto; font-size: .84rem; margin: .8rem 0; }
  pre code { background: none; color: inherit; padding: 0; font-size: inherit; }
  hr { border: none; border-top: 1px solid #c5cae9; margin: 2rem 0; }
  blockquote {
    border-left: 4px solid #ffa000;
    background: #fff8e1;
    padding: .7rem 1rem;
    margin: 1rem 0;
    border-radius: 0 6px 6px 0;
    color: #5d4037;
  }
  blockquote p { margin: .3rem 0; }
  blockquote strong { color: #e65100; }

  /* ── Tables ── */
  table {
    border-collapse: collapse;
    width: 100%;
    margin: 1rem 0;
    font-size: .88rem;
    background: #fff;
    box-shadow: 0 1px 4px rgba(0,0,0,.07);
    border-radius: 6px;
    overflow: hidden;
  }
  thead tr { background: #3949ab; color: #fff; }
  thead th { padding: .6rem .9rem; text-align: left; font-weight: 600; white-space: nowrap; }
  tbody tr:nth-child(even) { background: #f3f4fb; }
  tbody tr:hover { background: #e8eaf6; }
  tbody td { padding: .55rem .9rem; border-bottom: 1px solid #e0e4ef; vertical-align: top; }
  tbody tr:last-child td { border-bottom: none; }

  /* ── Gate row highlighting ── */
  tbody tr:has(td .gate-marker) { background: #fff8e1 !important; }
  tbody tr:has(td .gate-marker):hover { background: #fff3cd !important; }

  /* ── Gate marker ── */
  .gate-marker {
    display: inline-block;
    font-size: 1.1em;
    filter: drop-shadow(0 1px 1px rgba(0,0,0,.2));
  }

  /* ── Status badges ── */
  .badge {
    display: inline-block;
    padding: .18em .55em;
    border-radius: 12px;
    font-size: .78em;
    font-weight: 700;
    letter-spacing: .03em;
    vertical-align: middle;
    white-space: nowrap;
  }
  .badge-confirmed { background: #e8f5e9; color: #2e7d32; border: 1px solid #a5d6a7; }
  .badge-draft { background: #fff8e1; color: #f57f17; border: 1px solid #ffe082; }
  .badge-open { background: #fce4ec; color: #c62828; border: 1px solid #ef9a9a; }

  /* ── Diagram containers ── */
  .diagram-wrap {
    background: #fff;
    border: 1px solid #c5cae9;
    border-radius: 8px;
    padding: 1.2rem 1.5rem;
    margin: 1.5rem 0;
    box-shadow: 0 2px 8px rgba(0,0,0,.06);
  }
  pre.mermaid {
    background: transparent;
    color: inherit;
    padding: 0;
    font-family: inherit;
    font-size: 1rem;
    overflow: visible;
    white-space: pre;
  }

  /* ── Scrollbar ── */
  .toc::-webkit-scrollbar { width: 4px; }
  .toc::-webkit-scrollbar-thumb { background: #c5cae9; border-radius: 2px; }

  /* ── Print / narrow ── */
  @media (max-width: 860px) {
    .toc { display: none; }
    .main { padding: 1.5rem 1.2rem 3rem; }
  }
"""


def render(plan_path: Path) -> None:
    if not plan_path.exists():
        print(f"ERROR: plan.md not found: {plan_path}", file=sys.stderr)
        sys.exit(1)
    if not plan_path.is_file():
        print(f"ERROR: not a file: {plan_path}", file=sys.stderr)
        sys.exit(1)

    try:
        source = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read {plan_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    lines = source.splitlines()

    # Extract metadata for the header band
    meta = extract_meta(lines)
    headings = extract_headings(lines)

    # Determine page title: first # heading or filename stem
    title = next(
        (text for level, text, _ in headings if level == 1),
        plan_path.stem,
    )

    # Build TOC HTML
    toc_html = build_toc(headings)

    # Convert markdown — intercept mermaid blocks first
    mermaid_blocks: list[str] = []
    mermaid_ext = MermaidExtension(mermaid_blocks)
    md = markdown.Markdown(extensions=[mermaid_ext, "tables", "fenced_code"])
    body_html = md.convert(source)

    # Post-process
    body_html = inject_anchors(body_html, headings)
    body_html = insert_mermaid(body_html, mermaid_blocks)
    body_html = apply_gate_markers(body_html)
    body_html = apply_badges(body_html)

    # Count artefacts
    table_count = body_html.count("<table")
    mermaid_count = len(mermaid_blocks)

    # Build status badge for header
    status_text = html.escape(meta.get("status", ""))
    # Determine badge class for status
    status_upper = status_text.upper()
    if "DRAFT" in status_upper:
        status_html = f'<span class="badge badge-draft">{status_text}</span>'
    elif "OPEN" in status_upper:
        status_html = f'<span class="badge badge-open">{status_text}</span>'
    elif "CONFIRMED" in status_upper:
        status_html = f'<span class="badge badge-confirmed">{status_text}</span>'
    else:
        status_html = f"<strong>{status_text}</strong>"

    campaign = html.escape(meta.get("campaign", ""))
    owner = html.escape(meta.get("owner", ""))
    date = html.escape(meta.get("date", ""))
    title_escaped = html.escape(title)

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_escaped}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<script>mermaid.initialize({{startOnLoad:true, theme:'base', themeVariables:{{primaryColor:'#e8f0fe', primaryTextColor:'#1a1a2e', primaryBorderColor:'#4a6fa5', lineColor:'#4a6fa5', secondaryColor:'#fff3e0', tertiaryColor:'#f3e5f5'}}}});</script>
<style>
{CSS}
</style>
</head>
<body>

<header class="site-header">
  <div class="site-header-text">
    <h1>{title_escaped}</h1>
    <div class="meta">
      {"<span>&#128196; Campaign: <strong>" + campaign + "</strong></span>" if campaign else ""}
      {"<span>&#128100; Owner: <strong>" + owner + "</strong></span>" if owner else ""}
      {"<span>&#128197; Date: <strong>" + date + "</strong></span>" if date else ""}
      {"<span>&#128993; Status: " + status_html + "</span>" if status_text else ""}
    </div>
  </div>
</header>

<div class="layout">
  <nav class="toc" aria-label="Table of contents">
    <h2>Contents</h2>
    <ul>
{toc_html}
    </ul>
  </nav>

  <main class="main">
{body_html}
  </main>
</div>

</body>
</html>
"""

    out_path = plan_path.parent / "plan.html"
    out_path.write_text(html_out, encoding="utf-8")

    print(f"Output:  {out_path}")
    print(f"Tables:  {table_count}")
    print(f"Mermaid: {mermaid_count}")


def main():
    if len(sys.argv) < 2:
        print("Usage: render_plan.py <abs-path-to-plan.md>", file=sys.stderr)
        sys.exit(1)
    plan_path = Path(sys.argv[1]).resolve()
    render(plan_path)


if __name__ == "__main__":
    main()
