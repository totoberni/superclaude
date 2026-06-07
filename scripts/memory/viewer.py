#!/usr/bin/env python3
"""
viewer.py — Browsable HTML viewer for the v3 memory subsystem.

Generates a self-contained HTML page from the memory DB for either:
  * a SINGLE memory (by id or name), or
  * a LIST / search-result set (by --search query, --type, or all).

Single-memory pages reuse memory_db.get_html (the cached, self-contained
per-row fragment). MULTI-memory pages re-render each memory's `text` through
ONE shared render.ScriptRegistry so every CDN <script> (mermaid.js, viz.js,
vega-embed, marked.js) is included exactly once per page — they deliberately
do NOT stitch the cached per-row html (which is self-contained and would
duplicate every script N times). Server-side TikZ SVGs are inlined regardless.

The page layout is one <section> per memory: an <h2> name + a description line
+ a <details> collapsible holding the rendered body and a metadata footer.

GATE mode (`--gate`) writes a proof-of-pipeline page to
``~/.claude/agent-memory/.viewer/gate.html`` containing 2-3 real migrated
memories rendered from the DB, an injected quantikz 2-qubit-circuit TikZ
diagram (proving server-side TikZ→SVG end-to-end), and a Mermaid block.

Always run under ~/.claude/.venv/bin/python.

Usage:
    python viewer.py --id 18                 -> stdout
    python viewer.py --name push_false_no_commit -o out.html
    python viewer.py --search "git push"     -> stdout
    python viewer.py --type feedback -k 20 -o feedback.html
    python viewer.py --all -o all.html
    python viewer.py --gate                  -> writes the gate artifact
"""

from __future__ import annotations

import argparse
import html as _html
import re
import sqlite3
import sys
from pathlib import Path

# Sibling modules live next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory_db  # noqa: E402  (path injected above)
import render  # noqa: E402

GATE_PATH = Path.home() / ".claude" / "agent-memory" / ".viewer" / "gate.html"

# Mirror memory_db's notation detection so multi-memory pages segment the same
# way while threading a shared ScriptRegistry. (Single source of the regex/kind
# set is memory_db; we reuse its compiled pattern and kind set directly.)
_FENCE_RE = memory_db._FENCE_RE
_NOTATION_KINDS = memory_db._NOTATION_KINDS


# --------------------------------------------------------------------------- #
# Body rendering with a shared script registry (page-level dedup)
# --------------------------------------------------------------------------- #


def render_body(text: str, scripts: render.ScriptRegistry) -> str:
    """Render a memory body to HTML, threading a shared ScriptRegistry.

    Splits the markdown into prose + embedded-notation segments exactly like
    memory_db._render_text, but passes ``scripts=`` to every render call so
    each CDN <script> is emitted once per page. Used for MULTI-memory pages;
    do NOT use the cached per-row html for these (it is self-contained and
    would repeat each CDN script for every row).
    """
    text = text or ""
    parts: list[str] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        kind = m.group("kind").lower()
        if kind not in _NOTATION_KINDS:
            continue  # e.g. ```python — leave in the markdown stream
        prose = text[pos:m.start()]
        if prose.strip():
            parts.append(render.render(prose, "markdown", scripts=scripts))
        parts.append(render.render(m.group("body"), kind, scripts=scripts))
        pos = m.end()

    tail = text[pos:]
    if not parts:
        return render.render(text, "markdown", scripts=scripts)
    if tail.strip():
        parts.append(render.render(tail, "markdown", scripts=scripts))
    return "".join(parts)


# --------------------------------------------------------------------------- #
# DB row selection
# --------------------------------------------------------------------------- #


def _fetch_by_id(mem_id: int, db_path) -> list[dict]:
    conn = memory_db._connect(db_path)
    try:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
        return [dict(row)] if row else []
    finally:
        conn.close()


def _fetch_by_name(name: str, db_path) -> list[dict]:
    conn = memory_db._connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM memories WHERE name = ? ORDER BY id", (name,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _fetch_by_type(mtype: str, k: int, db_path) -> list[dict]:
    conn = memory_db._connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM memories WHERE type = ? ORDER BY name LIMIT ?",
            (mtype, k),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _fetch_all(k: int, db_path) -> list[dict]:
    conn = memory_db._connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM memories ORDER BY tier, type, name LIMIT ?", (k,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Page assembly
# --------------------------------------------------------------------------- #

_PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  max-width: 60rem; margin: 0 auto; padding: 2rem 1.25rem 5rem;
  color: #1a1a1a; background: #fafafa;
}
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
.page-sub { color: #666; margin: 0 0 2rem; font-size: .9rem; }
section.mem { background: #fff; border: 1px solid #e2e2e2; border-radius: 10px;
  margin: 0 0 1rem; padding: 1rem 1.25rem; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
section.mem > h2 { font-size: 1.15rem; margin: 0 0 .15rem; }
.mem-desc { color: #555; font-size: .92rem; margin: 0 0 .35rem; }
.mem-meta { display: flex; flex-wrap: wrap; gap: .4rem; margin: .25rem 0 0;
  font-size: .72rem; color: #777; }
.mem-meta .tag { background: #eef1f5; border-radius: 999px; padding: .1rem .55rem; }
.mem-meta .tag.type-feedback { background: #e6f0ff; }
.mem-meta .tag.type-project  { background: #e9f7ec; }
.mem-meta .tag.type-index    { background: #fdf0e3; }
details { margin: .5rem 0 0; }
details > summary { cursor: pointer; font-size: .85rem; color: #36c;
  user-select: none; list-style: revert; }
.mem-body { margin-top: .75rem; padding-top: .75rem; border-top: 1px dashed #e4e4e4;
  overflow-x: auto; }
.mem-body pre { background: #f3f3f3; padding: .6rem .8rem; border-radius: 6px;
  overflow-x: auto; font-size: .85em; }
.mem-body code { background: #f3f3f3; padding: .08em .3em; border-radius: 4px; }
.mem-tikz, .mem-graphviz, .mem-vega { margin: .9rem 0; text-align: center; }
.mem-tikz svg { max-width: 100%; height: auto; }
pre.mermaid { background: transparent; text-align: center; }
.mem-render-fallback pre { background: #fff4f4; }
.mem-render-note { color: #a33; font-size: .78em; }
@media (prefers-color-scheme: dark) {
  body { color: #e6e6e6; background: #161616; }
  section.mem { background: #1f1f1f; border-color: #333; }
  .mem-desc { color: #aaa; } .page-sub { color: #999; }
  .mem-body { border-top-color: #333; }
  .mem-body pre, .mem-body code { background: #2a2a2a; }
  .mem-meta .tag { background: #2c2c2c; color: #bbb; }
}
"""


def _meta_tags(m: dict) -> str:
    bits = []
    for key in ("type", "tier", "agent"):
        val = m.get(key)
        if val:
            cls = f"tag type-{val}" if key == "type" else "tag"
            bits.append(f'<span class="{cls}">{_html.escape(str(val))}</span>')
    if m.get("path"):
        leaf = Path(str(m["path"])).name
        bits.append(f'<span class="tag">{_html.escape(leaf)}</span>')
    return "".join(bits)


def _section(m: dict, body_html: str, *, open_details: bool) -> str:
    name = _html.escape(m.get("name") or f"memory {m.get('id')}")
    desc = m.get("description") or ""
    desc_html = f'<p class="mem-desc">{_html.escape(desc)}</p>' if desc else ""
    summary = "Hide body" if open_details else "Show body"
    opn = " open" if open_details else ""
    return (
        '<section class="mem">'
        f"<h2>{name}</h2>"
        f"{desc_html}"
        f'<div class="mem-meta">{_meta_tags(m)}</div>'
        f"<details{opn}><summary>{summary}</summary>"
        f'<div class="mem-body">{body_html}</div>'
        "</details>"
        "</section>"
    )


def build_page(rows: list[dict], title: str, *, db_path) -> str:
    """Assemble a full HTML page for the given memory rows.

    Single row: reuse memory_db.get_html (cached self-contained fragment) when
    the row has an id and we can hit the DB. Multiple rows: thread ONE shared
    ScriptRegistry through render_body so CDN scripts are deduped page-wide.
    """
    if len(rows) == 1 and rows[0].get("id") is not None:
        body = memory_db.get_html(rows[0]["id"], db_path=db_path) or memory_db._pre_fallback(
            rows[0].get("text", "")
        )
        sections = _section(rows[0], body, open_details=True)
    else:
        scripts = render.ScriptRegistry()
        sections = "".join(
            _section(m, render_body(m.get("text", ""), scripts), open_details=False)
            for m in rows
        )

    n = len(rows)
    sub = f"{n} memor{'y' if n == 1 else 'ies'}"
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_html.escape(title)}</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        f"<h1>{_html.escape(title)}</h1>"
        f'<p class="page-sub">{sub}</p>'
        f"{sections}"
        "</body></html>"
    )


# --------------------------------------------------------------------------- #
# Gate artifact
# --------------------------------------------------------------------------- #

# Injected proof snippets. The quantikz 2-qubit circuit forces a server-side
# TikZ→SVG compile through the real pipeline; the mermaid block forces a client
# block + CDN script. These ride inside a synthetic memory body alongside real
# DB memories so the page demonstrates the full embedded-notation path.
_GATE_TIKZ = r"""\begin{quantikz}
\lstick{$\ket{0}$} & \gate{H} & \ctrl{1} & \qw \\
\lstick{$\ket{0}$} & \qw      & \targ{}  & \qw
\end{quantikz}"""

_GATE_MERMAID = """graph LR
  MD["flat .md (SOT)"] -->|migrate| DB[(.memory.db)]
  DB -->|get_html| V["HTML viewer"]
  DB -->|search| A["agents"]"""


def _gate_proof_memory() -> dict:
    """A synthetic memory whose body embeds the injected TikZ + Mermaid blocks."""
    text = (
        "## Render-pipeline proof\n\n"
        "This memory is **synthetic** — it exists to prove the viewer renders "
        "embedded notation. Below: a server-side **quantikz** 2-qubit circuit "
        "(Bell-state prep) compiled to inline SVG, then a client-side "
        "**Mermaid** dataflow diagram.\n\n"
        "```{tikz}\n" + _GATE_TIKZ + "\n```\n\n"
        "Memory subsystem dataflow:\n\n"
        "```mermaid\n" + _GATE_MERMAID + "\n```\n\n"
        "If you can see the circuit SVG above, server-side TikZ works end-to-end."
    )
    return {
        "id": None,
        "name": "GATE PROOF — embedded TikZ + Mermaid",
        "description": "Synthetic: quantikz 2-qubit circuit (server SVG) + Mermaid dataflow (client).",
        "type": "index",
        "tier": "gate",
        "agent": None,
        "path": None,
        "text": text,
    }


def _pick_real_memories(db_path, n: int = 3) -> list[dict]:
    """Pick a few short, readable, real migrated memories for the gate page."""
    conn = memory_db._connect(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM memories
               WHERE length(text) BETWEEN 250 AND 1100
               ORDER BY type DESC, length(text)
               LIMIT ?""",
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def build_gate(db_path) -> tuple[str, int]:
    """Build the gate page: real memories + injected TikZ + Mermaid proof.

    Threads one shared ScriptRegistry across all sections (multi-memory page).
    Returns (html, n_real_memories).
    """
    real = _pick_real_memories(db_path, 3)
    proof = _gate_proof_memory()

    scripts = render.ScriptRegistry()
    # Proof first (it carries the TikZ + Mermaid), then real memories.
    ordered = [proof] + real
    sections = "".join(
        _section(m, render_body(m.get("text", ""), scripts), open_details=True)
        for m in ordered
    )
    title = "Memory Viewer — Gate Artifact"
    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_html.escape(title)}</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        f"<h1>{_html.escape(title)}</h1>"
        '<p class="page-sub">'
        f"{len(real)} real migrated memories + 1 synthetic render-pipeline proof "
        "(server-side TikZ SVG + client Mermaid)."
        "</p>"
        f"{sections}"
        "</body></html>"
    )
    return page, len(real)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _select_rows(args, db_path) -> tuple[list[dict], str]:
    """Resolve CLI selectors to (rows, page_title)."""
    if args.id is not None:
        return _fetch_by_id(args.id, db_path), f"Memory #{args.id}"
    if args.name:
        return _fetch_by_name(args.name, db_path), f"Memory: {args.name}"
    if args.search:
        rows = memory_db.search(args.search, k=args.k, mode=args.mode, db_path=db_path)
        return rows, f"Search: {args.search!r}"
    if args.type:
        return _fetch_by_type(args.type, args.k, db_path), f"Memories: type={args.type}"
    if args.all:
        return _fetch_all(args.k, db_path), "All memories"
    return [], ""


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="HTML viewer for the v3 memory DB.")
    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--id", type=int, help="render a single memory by id")
    sel.add_argument("--name", help="render memory/memories by exact name")
    sel.add_argument("--search", help="render the search-result set for a query")
    sel.add_argument("--type", help="render memories of a given type")
    sel.add_argument("--all", action="store_true", help="render all memories")
    sel.add_argument("--gate", action="store_true", help="write the gate proof artifact")
    p.add_argument("-k", type=int, default=12, help="max rows for list/search (default 12)")
    p.add_argument("--mode", default="hybrid", choices=("fts", "vec", "hybrid"),
                   help="search mode (default hybrid)")
    p.add_argument("-o", "--out", help="output file (default: stdout; gate ignores this)")
    p.add_argument("--db", default=str(memory_db.DB_PATH), help="DB path override")
    args = p.parse_args(argv)

    db_path = args.db

    if args.gate:
        page, n_real = build_gate(db_path)
        GATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        GATE_PATH.write_text(page, encoding="utf-8")
        has_svg = "<svg" in page
        has_mermaid = 'class="mermaid"' in page
        print(f"gate written → {GATE_PATH}")
        print(f"  real memories: {n_real} | inline <svg>: {has_svg} | mermaid block: {has_mermaid}")
        return 0 if (has_svg and has_mermaid and n_real >= 2) else 1

    rows, title = _select_rows(args, db_path)
    if not args.id and not args.name and not args.search and not args.type and not args.all:
        p.error("one of --id/--name/--search/--type/--all/--gate is required")
    if not rows:
        sys.stderr.write("no matching memories\n")
        return 1

    page = build_page(rows, title, db_path=db_path)
    if args.out:
        Path(args.out).write_text(page, encoding="utf-8")
        sys.stderr.write(f"wrote {len(rows)} memor{'y' if len(rows)==1 else 'ies'} → {args.out}\n")
    else:
        sys.stdout.write(page)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
