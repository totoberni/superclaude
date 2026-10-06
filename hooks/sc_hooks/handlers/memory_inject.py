"""memory-inject: puntatore alla memoria persistente (SessionStart e SubagentStart).

Emette sempre un puntatore statico e imperativo al DB di memoria (comando `mem`).
Solo su SessionStart aggiunge una "recovery slice": i 4 risultati FTS migliori
per una query costruita dal nome dell'agente più i termini di recupero contesto.

La query FTS gira direttamente con sqlite3 della libreria standard, in sola
lettura. La tabella `memories_vec` non viene mai toccata (la sua estensione non
è caricata qui). DB mancante, vuoto o qualsiasi errore sqlite: solo puntatore.
"""
from __future__ import annotations

from sc_hooks import core
from sc_hooks.core import Ctx, Result, on

DB_PATH = core.sc_profile.memory_db_path(core.PROFILE)

MAX_TOTAL = 9800        # tetto complessivo del testo iniettato
MAX_SLICE = 4000        # tetto della recovery slice
SLICE_K = 4
RECOVERY_TERMS = "recovery context current state handoff"

POINTER = (
    "MEMORY (read this first): your persistent memory is a hybrid-search SQLite DB at "
    f"~/{core.DATA_HOME.name}/agent-memory/.memory.db, queried with the `mem` command. There is NO MEMORY.md "
    "and there are NO MD memory files to read. BEFORE you rely on any assumption about a "
    "project, library, tool, convention, or past decision, FIRST query it:\n"
    '  mem search "<your topic> gotchas" -k 8\n'
    "then: mem get <name> for a full entry; mem similar <name> for near-duplicate or related "
    "rows; mem list --tier <t>. (mem sets its own offline environment; no prefix needed.) "
    "Tiers: instance/<agent>, shared-projects, shared-global, class. To write or update memory, "
    "use the memory skills (/remember, /good-idea, /lt-mem, /mistake); never hand-write .md "
    "memory files."
)


def fts_match_query(query: str) -> str:
    """Stringa MATCH sicura per FTS5: token alfanumerici tra virgolette, uniti da OR.

    Stesso approccio di memory_db._fts_match_query: la punteggiatura e gli
    operatori FTS5 nel testo libero non possono più rompere la sintassi.
    """
    import re
    tokens = re.findall(r"[A-Za-z0-9_]+", query or "")
    return " OR ".join(f'"{t}"' for t in tokens)


def fts_search(query: str, k: int = SLICE_K) -> list[dict]:
    """Migliori `k` righe per bm25, tier 'archive' escluso. Lista vuota su ogni errore."""
    match = fts_match_query(query)
    if not match:
        return []
    try:
        if not DB_PATH.is_file():
            return []
    except OSError:
        return []
    import sqlite3
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        rows = conn.execute(
            "SELECT m.id, m.name, m.tier, m.type, m.path, m.text, bm25(memories_fts) AS score "
            "FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
            "WHERE memories_fts MATCH ? AND COALESCE(m.tier, '') != 'archive' "
            "ORDER BY score LIMIT ?",
            (match, int(k)),
        ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    keys = ("id", "name", "tier", "type", "path", "text", "score")
    return [dict(zip(keys, r)) for r in rows]


def _format(rows: list[dict]) -> str:
    """Stesso layout testuale di `mem search` (id, nome, tier, tipo, path, estratto)."""
    out: list[str] = []
    for r in rows:
        snippet = " ".join(str(r.get("text") or "").split())[:120]
        out.append(
            f"[{r.get('id')}] {r.get('name') or '-'}  tier={r.get('tier') or '-'}"
            f"  type={r.get('type') or '-'}\n"
            f"       path: {r.get('path') or '-'}\n"
            f"       {snippet}"
        )
    return "\n\n".join(out)


def _inject(ctx: Ctx, with_slice: bool) -> Result:
    text = POINTER
    if with_slice:
        agent = ctx.agent if isinstance(ctx.agent, str) else ""
        rows = fts_search(f"{agent} {RECOVERY_TERMS}".strip())
        if rows:
            body = _format(rows)[:MAX_SLICE]
            text = (
                f"{POINTER}\n\nMost relevant stored memories for agent={agent or 'plain'} "
                f"(query the DB for more):\n{body}"
            )
    return Result.note(text[:MAX_TOTAL])


@on("SessionStart", name="memory-inject")
def on_session_start(ctx: Ctx) -> Result | None:
    return _inject(ctx, with_slice=True)


@on("SubagentStart", name="memory-inject")
def on_subagent_start(ctx: Ctx) -> Result | None:
    # I subagent hanno un compito mirato: la slice generica sarebbe solo rumore
    return _inject(ctx, with_slice=False)
