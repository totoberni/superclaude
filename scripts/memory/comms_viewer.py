#!/usr/bin/env python3
"""comms_viewer.py — Standalone-HTML emitter for v3 comms completion reports.

Renders agent-authored completion reports and comms entries (from the comms
search store, ``comms_db.py``) to self-contained in-browser HTML with embedded
Mermaid / TikZ / Vega / DOT notation. Terminal comms stay markdown on the bus;
this view is for FINAL / completion reports only.

This is a THIN emitter layered over the existing render pipeline — it does NOT
re-implement markdown/notation rendering or the CDN ``ScriptRegistry``:
  * ``comms_db.get_html(id)``  — cached, self-contained per-row fragment (used
    for the single-entry ``--id`` page).
  * ``viewer.render_body / _section / _PAGE_CSS / _meta_tags`` — the memory
    viewer's MULTI-row machinery. A bundle page threads ONE shared
    ``render.ScriptRegistry`` through ``viewer.render_body`` so every CDN
    ``<script>`` (marked.js, mermaid.js, vega-embed, viz.js) is emitted exactly
    once per page, not once-per-row. Server-side TikZ SVGs are inlined regardless.
  * ``memory_db._connect`` — the one canonical connection helper (loads
    sqlite-vec). The tiny read-by-agent/kind helper below reuses it; it does NOT
    duplicate connection logic.

Modes (argparse, mutually exclusive selector):
  --id N            one comms entry as a full standalone HTML page
                    (reuses comms_db.get_html(N)).
  --agent X         COMPLETION-REPORT BUNDLE: all comms rows for agent X
    [--kind RPT]    (optionally one kind), newest-first, one shared-registry page.
  --search Q        search hits rendered as a bundle page.
    [--mode ...]
  --demo            a SYNTHETIC agent-authored completion report whose body
                    embeds a Mermaid + TikZ + Vega block + prose — proving a
                    completion report renders in-browser with a chart +
                    TikZ/Mermaid (Phase-2 completion criterion). Mirrors
                    viewer.py --gate.

Default ``--out`` = ``~/.claude/comms/.viewer/report.html`` (dir is mkdir -p'd).
The written path is printed.

Always run under ~/.claude/.venv/bin/python with HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1 (only --search embeds the query; the env is set anyway).
The broker and comms DB are READ-ONLY sources; this script never writes them
(``get_html`` caches into the comms DB via comms_db's own write path — that is
the reused lazy-cache, not a new mutation).
"""

from __future__ import annotations

import argparse
import html as _html
import sys
from pathlib import Path

# Sibling modules live next to this file; inject the dir BEFORE importing them
# so the imports resolve regardless of CWD (agents run from ~/projects/workspace/).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import comms_db  # noqa: E402  (path injected above)
import memory_db  # noqa: E402
import render  # noqa: E402
import viewer  # noqa: E402  (reuse render_body / _section / _PAGE_CSS / _meta_tags)

DEFAULT_OUT = Path.home() / ".claude" / "comms" / ".viewer" / "report.html"


# --------------------------------------------------------------------------- #
# DB row selection (reuses memory_db._connect — no new connection logic)
# --------------------------------------------------------------------------- #


def _fetch_one(mem_id: int, db_path) -> dict | None:
    """Fetch a single comms row by id, or None if absent."""
    conn = memory_db._connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _fetch_by_agent(agent: str, kind: str | None, db_path) -> list[dict]:
    """All comms rows for an agent (optionally one kind), newest-first.

    Newest-first is by descending broker id, which is the monotonic global PK
    the comms store keys on (path = ``broker/<id>``). Restricted to
    broker-sourced rows so the bundle never mixes in non-comms content.
    """
    conn = memory_db._connect(db_path)
    try:
        if kind:
            rows = conn.execute(
                "SELECT * FROM memories "
                "WHERE path LIKE 'broker/%' AND agent = ? AND type = ? "
                "ORDER BY id DESC",
                (agent, kind),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories "
                "WHERE path LIKE 'broker/%' AND agent = ? "
                "ORDER BY id DESC",
                (agent,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Page assembly
# --------------------------------------------------------------------------- #


def _document(title: str, subtitle: str, sections: str) -> str:
    """Wrap rendered sections in a full standalone HTML document.

    Reuses viewer._PAGE_CSS so the comms view and the memory view share one
    stylesheet (single source of the page CSS).
    """
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_html.escape(title)}</title>"
        f"<style>{viewer._PAGE_CSS}</style></head><body>"
        f"<h1>{_html.escape(title)}</h1>"
        f'<p class="page-sub">{_html.escape(subtitle)}</p>'
        f"{sections}"
        "</body></html>"
    )


def build_single(row: dict, db_path) -> str:
    """One comms entry as a standalone page (reuses comms_db.get_html).

    get_html returns the cached self-contained fragment (every CDN script it
    needs is inline), so a single-row page does NOT need a shared registry.
    """
    body = comms_db.get_html(row["id"])
    if body is None:  # extremely unlikely (row was just fetched) — degrade safely
        body = memory_db._pre_fallback(row.get("text", ""))
    title = f"Comms #{row['id']} — {row.get('name') or ''}".strip()
    subtitle = row.get("description") or "1 comms entry"
    section = viewer._section(row, body, open_details=True)
    return _document(title, subtitle, section)


def build_bundle(rows: list[dict], title: str, subtitle: str) -> str:
    """A multi-row bundle page with ONE shared ScriptRegistry (page-wide dedup).

    Mirrors viewer.py's multi-memory mode: each row's body is rendered via
    viewer.render_body(text, scripts) threading the SAME registry, so every CDN
    <script> is emitted once per page rather than once per row. This is the
    subagent/orch completion-report view.
    """
    scripts = render.ScriptRegistry()
    sections = "".join(
        viewer._section(r, viewer.render_body(r.get("text", ""), scripts),
                        open_details=False)
        for r in rows
    )
    return _document(title, subtitle, sections)


# --------------------------------------------------------------------------- #
# Demo / completion-criterion proof (mirrors viewer.py --gate)
# --------------------------------------------------------------------------- #

# A synthetic agent-authored completion report. Its body embeds all three
# notations the Phase-2 criterion calls for: a server-side TikZ block (forces a
# real latexmk→dvisvgm compile to inline SVG), a client-side Mermaid diagram,
# and a Vega chart — interleaved with report prose. Mirrors viewer.py's gate
# proof memory.
_DEMO_TIKZ = r"""\begin{tikzpicture}[>=Stealth, node distance=1.6cm]
  \node[draw, rounded corners] (dir) {DIR};
  \node[draw, rounded corners, right=of dir] (work) {worker};
  \node[draw, rounded corners, right=of work] (rpt) {RPT};
  \draw[->] (dir) -- (work);
  \draw[->] (work) -- (rpt);
\end{tikzpicture}"""

_DEMO_MERMAID = """graph LR
  BROKER[(HCOM broker)] -->|sync| DB[(.comms.db)]
  DB -->|get_html| V["comms_viewer"]
  V -->|standalone HTML| R["completion report"]"""

_DEMO_VEGA = """{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "description": "Tasks completed per phase (synthetic).",
  "data": {"values": [
    {"phase": "P1", "tasks": 6},
    {"phase": "P2", "tasks": 9},
    {"phase": "P3", "tasks": 4}
  ]},
  "mark": "bar",
  "encoding": {
    "x": {"field": "phase", "type": "nominal"},
    "y": {"field": "tasks", "type": "quantitative"}
  }
}"""


def _demo_report() -> dict:
    """A synthetic completion-report row whose body embeds TikZ+Mermaid+Vega."""
    text = (
        "## Completion Report — synthetic\n\n"
        "This is a **synthetic agent-authored completion report**. It exists to "
        "prove a completion report renders in-browser with an embedded chart, "
        "TikZ, and Mermaid via the v3 comms HTML pipeline.\n\n"
        "### Pipeline (server-side TikZ → inline SVG)\n\n"
        "```{tikz}\n" + _DEMO_TIKZ + "\n```\n\n"
        "### Dataflow (client-side Mermaid)\n\n"
        "```mermaid\n" + _DEMO_MERMAID + "\n```\n\n"
        "### Throughput (client-side Vega-Lite)\n\n"
        "```vega\n" + _DEMO_VEGA + "\n```\n\n"
        "If you can see the SVG diagram, the dataflow graph, and the bar chart "
        "above, then a completion report renders end-to-end with all three "
        "notations on ONE page."
    )
    return {
        "id": None,
        "name": "DEMO RPT — embedded TikZ + Mermaid + Vega",
        "description": "Synthetic completion report: server TikZ SVG + Mermaid + Vega chart.",
        "type": "RPT",
        "tier": "comms",
        "agent": "demo-agent",
        "path": None,
        "text": text,
    }


def build_demo() -> tuple[str, bool, bool, bool]:
    """Build the demo page. Returns (html, has_svg, has_mermaid, has_vega).

    Single synthetic row, rendered through one shared registry (same code path
    as a real bundle). The booleans are the completion-criterion probes.
    """
    page = build_bundle(
        [_demo_report()],
        "Comms Viewer — Demo Completion Report",
        "1 synthetic completion report proving in-browser TikZ + Mermaid + Vega.",
    )
    has_svg = "<svg" in page
    has_mermaid = 'class="mermaid"' in page
    has_vega = 'class="mem-vega"' in page
    return page, has_svg, has_mermaid, has_vega


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def _write(page: str, out: str | None) -> Path:
    """Write the page to *out* (or the default), mkdir -p'ing the dir. Returns path."""
    dest = Path(out) if out else DEFAULT_OUT
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(page, encoding="utf-8")
    return dest


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="comms_viewer",
        description="Standalone-HTML emitter for v3 comms completion reports.",
    )
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--id", type=int, help="render one comms entry by id")
    sel.add_argument("--agent", help="render a completion-report bundle for an agent")
    sel.add_argument("--search", help="render the search-result set for a query")
    sel.add_argument("--demo", action="store_true",
                     help="render a synthetic completion report (criterion proof)")
    p.add_argument("--kind", help="with --agent: filter to one kind (e.g. RPT, DIR)")
    p.add_argument("-k", type=int, default=12, help="max hits for --search (default 12)")
    p.add_argument("--mode", default="hybrid", choices=("fts", "vec", "hybrid"),
                   help="--search mode (default hybrid)")
    p.add_argument("-o", "--out", help=f"output file (default: {DEFAULT_OUT})")
    args = p.parse_args(argv)

    # --demo needs no DB; the others do. Fail-safe on a missing comms DB.
    if not args.demo and not comms_db.COMMS_DB.exists():
        sys.stderr.write(
            f"error: comms DB not found at {comms_db.COMMS_DB} — "
            "run `comms_db.py sync` first.\n"
        )
        return 1

    db_path = comms_db.COMMS_DB

    if args.demo:
        page, has_svg, has_mermaid, has_vega = build_demo()
        dest = _write(page, args.out)
        print(f"demo report written → {dest}  ({len(page)} bytes)")
        print(f"  inline <svg>: {has_svg} | mermaid block: {has_mermaid} | "
              f"vega block: {has_vega}")
        # Mermaid + Vega are CDN/client and always present; TikZ SVG requires the
        # latexmk/dvisvgm toolchain. Treat all three present as full success;
        # exit non-zero only if the client blocks (which never need a toolchain)
        # are somehow missing — a real pipeline break.
        if not (has_mermaid and has_vega):
            return 1
        if not has_svg:
            sys.stderr.write(
                "warning: no inline <svg> — TikZ toolchain (latexmk/dvisvgm) "
                "unavailable; TikZ degraded to <pre>. Mermaid + Vega OK.\n"
            )
        return 0

    if args.id is not None:
        row = _fetch_one(args.id, db_path)
        if row is None:
            sys.stderr.write(f"error: no comms row with id={args.id}.\n")
            return 1
        dest = _write(build_single(row, db_path), args.out)
        print(f"comms #{args.id} written → {dest}")
        return 0

    if args.agent is not None:
        rows = _fetch_by_agent(args.agent, args.kind, db_path)
        if not rows:
            filt = f" kind={args.kind}" if args.kind else ""
            sys.stderr.write(
                f"no comms rows for agent={args.agent!r}{filt}.\n"
            )
            return 0  # an empty bundle is not an error
        kind_lbl = f" / {args.kind}" if args.kind else ""
        title = f"Completion reports — {args.agent}{kind_lbl}"
        subtitle = f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}, newest first"
        dest = _write(build_bundle(rows, title, subtitle), args.out)
        print(f"bundle ({len(rows)} rows) written → {dest}")
        return 0

    if args.search is not None:
        rows = comms_db.search(args.search, k=args.k, mode=args.mode)
        if not rows:
            sys.stderr.write(
                f"no results for {args.search!r} (mode={args.mode}).\n"
            )
            return 0  # empty results are not an error
        title = f"Comms search — {args.search!r}"
        subtitle = f"{len(rows)} hit{'' if len(rows) == 1 else 's'} (mode={args.mode})"
        dest = _write(build_bundle(rows, title, subtitle), args.out)
        print(f"search ({len(rows)} hits) written → {dest}")
        return 0

    return 0  # unreachable (required mutually-exclusive group)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
