"""Deterministic implementation of the /mem-health rubric (6 criteria, 100 pts).

Usage: scpy memory/mem_health.py [--quick|--verbose]
  --quick   : skip the near-duplicate scan (criterion 3 auto-awards full 20)
  --verbose : append the per-tier signal table (from scan_mem_matrix.py)

Contract: prints a per-criterion breakdown and, as its FINAL stdout line, exactly
"SCORE: <int>/100". Exit code is ALWAYS 0 (reporting tool, not a gate).

Port Python di scripts/mem-health.sh. Stessi criteri, pesi, soglie, variabili
d'ambiente (MEM_OVERSIZE_BYTES, MEM_STALE_DAYS, MEM_DUP_SIM_MIN,
MEM_CORPUS_BYTES_MAX, MEM_INCOMPLETE_CAP, MEMORY_DB_PATH, CLAUDE_DIR) e stesso
formato di output. Solo `sqlite3` della libreria standard, connessione di sola
lettura, nessun modello di embedding e nessuna estensione vec.

Note sul port:
  - Nessun criterio dell'originale dipendeva da comms DB, timer di sessione o
    sync tra dispositivi: i 6 criteri e i pesi 20/20/20/15/15/10 sono invariati,
    quindi non e' servita alcuna rinormalizzazione aggiuntiva dei pesi.
  - Il controllo near-dup dell'originale e' un Jaccard sui token (awk), non usa
    embedding: qui e' riscritto in Python puro e memory_db NON viene importato.
  - Lo "Step 0" dell'originale lanciava lo scan scartandone l'output (nessun
    effetto sul punteggio): e' stato rimosso. La tabella resta in --verbose.
  - Il motivo di indisponibilita' "sqlite3 absent" non esiste piu' (nessuna CLI).
  - Unica differenza di formato: il separatore nelle righe di dettaglio e' un
    trattino ASCII (" - ") e non il trattino lungo dell'originale.

Criteri (dettagli in skills/mem-health/SKILL.md):
  1 Right-sized rows    20  righe non oversize (le budget-exempt non penalizzano)
  2 FTS index cohesion  20  memories == memories_fts_docsize, altrimenti proporzionale
  3 No near-dup rows    20  coppie con Jaccard >= DUP_SIM_MIN
  4 vec store soundness 15  memories_vec presente in sqlite_master
  5 Metadata complete   15  name + description + type non vuoti
  6 Staleness managed   10  N/A se l'edit-span del corpus e' sotto STALE_DAYS
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# sc_profile vive in <code home>/lib: il percorso si ricava da questo file, cosi'
# funziona sia dall'albero live sia da una copia di staging
_LIB_DIR = str(Path(__file__).resolve().parents[2] / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import sc_profile  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_mem_matrix as scan  # noqa: E402
from db_wall import CrossProfileDBError  # noqa: E402

# Separatore usato nelle righe di output. L'originale bash stampava un trattino
# lungo; la regola del repo vieta i trattini lunghi, quindi qui e' ASCII puro.
# Ogni uso e' gia' circondato da spazi, per cui le righe si leggono "a - b".
DASH = "-"

# Marcatore budget-exempt ancorato a inizio riga (inizio corpo o dopo un newline).
EXEMPT_PRED = (
    "(text LIKE '<!-- budget-exempt%' "
    "OR text LIKE '%' || char(10) || '<!-- budget-exempt%')"
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _db_path() -> Path:
    """MEMORY_DB_PATH, altrimenti <CLAUDE_DIR o data home del profilo>/agent-memory/.memory.db."""
    override = os.environ.get("MEMORY_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    claude = os.environ.get("CLAUDE_DIR", "").strip()
    base = Path(claude).expanduser() if claude else sc_profile.data_home()
    return base / "agent-memory" / ".memory.db"


def pct_points(ok: int, total: int, max_pts: int) -> int:
    """Punti proporzionali con divisione intera (come `pct_points` in bash)."""
    if total <= 0:
        return 0
    return ok * max_pts // total


def _token_set(text: str) -> set[str]:
    """Insieme dei token ASCII alfanumerici, minuscoli, lunghi almeno 3.

    Replica la pipeline LOWER() di SQLite + awk: solo le lettere ASCII vengono
    abbassate, ogni altro carattere fa da separatore.
    """
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) >= 3}


def count_near_dup_pairs(texts: list[str], sim_min: float) -> int:
    """Numero di coppie di corpi con Jaccard sui token >= sim_min."""
    sets = [s for s in (_token_set(t) for t in texts) if s]
    pairs = 0
    for i, a in enumerate(sets):
        for b in sets[i + 1:]:
            inter = len(a & b)
            union = len(a) + len(b) - inter
            if union > 0 and inter / union >= sim_min:
                pairs += 1
    return pairs


def report(mode: str = "normal", path: Path | None = None) -> list[str]:
    """Costruisce tutte le righe del report; l'ultima e' sempre `SCORE: N/100`."""
    path = _db_path() if path is None else path
    oversize = scan.env_int("MEM_OVERSIZE_BYTES", scan.DEFAULT_OVERSIZE_BYTES)
    stale_days = scan.env_int("MEM_STALE_DAYS", scan.DEFAULT_STALE_DAYS)
    dup_raw = os.environ.get("MEM_DUP_SIM_MIN", "").strip() or "0.82"
    try:
        dup_sim_min = float(dup_raw)
    except ValueError:
        dup_raw, dup_sim_min = "0.82", 0.82
    corpus_max = scan.env_int("MEM_CORPUS_BYTES_MAX", 1500000)
    incomplete_cap = scan.env_int("MEM_INCOMPLETE_CAP", 97)

    out = [f"## Memory Health Report (mode: {mode})", ""]
    head = ["| # | Criterion | Score | Detail |", "|---|-----------|-------|--------|"]

    conn, reason = scan.open_ro(path)
    if conn is None:
        if reason.startswith("DB not built"):
            reason = f"memory DB not built ({path})"
        else:
            reason = "memory DB is not valid SQLite"
        out.append(f"memory DB unavailable: {reason} {DASH} all criteria score 0.")
        out.append("")
        out.extend(head)
        for n, label, pts in (
            (1, "Right-sized rows", 20),
            (2, "FTS index cohesion", 20),
            (3, "No near-dup rows", 20),
            (4, "vec store soundness", 15),
            (5, "Metadata complete", 15),
            (6, "Staleness managed", 10),
        ):
            out.append(f"| {n} | {label} | 0/{pts} | {reason} |")
        out += ["", "SCORE: 0/100"]
        return out

    score = 0
    maxpts = 0
    try:
        total = scan.scalar(conn, "SELECT COUNT(*) FROM memories")
        out.extend(head)

        # Criterio 1: righe oversize non esenti; il denominatore resta il totale.
        big = f"LENGTH(text) > {oversize}"
        c1_over = scan.scalar(
            conn, f"SELECT COUNT(*) FROM memories WHERE {big} AND NOT {EXEMPT_PRED}"
        )
        c1_exempt = scan.scalar(
            conn, f"SELECT COUNT(*) FROM memories WHERE {big} AND {EXEMPT_PRED}"
        )
        fat_names = ",".join(
            (r[0] or "")
            for r in scan.q(
                conn,
                f"SELECT name FROM memories WHERE {big} AND NOT {EXEMPT_PRED} "
                "ORDER BY LENGTH(text) DESC LIMIT 5",
            )
        )
        if total == 0:
            c1, c1_detail = 0, "no rows in DB"
        else:
            c1 = pct_points(total - c1_over, total, 20)
            c1_detail = f"{total - c1_over}/{total} within {oversize}B"
            if fat_names:
                c1_detail += f"; fat: {fat_names}"
            c1_detail += f"; exempt: {c1_exempt}"
        score += c1
        maxpts += 20
        out.append(f"| 1 | Right-sized rows | {c1}/20 | {c1_detail} |")

        # Criterio 2: un documento FTS indicizzato per ogni riga di contenuto.
        c2_idx = scan.scalar(conn, "SELECT COUNT(*) FROM memories_fts_docsize")
        if total > 0 and c2_idx == total:
            c2 = 20
            c2_detail = f"fts index aligned (rows={total}, fts_idx={c2_idx})"
        else:
            c2 = pct_points(min(c2_idx, total), total, 20) if total > 0 else 0
            c2_detail = (
                f"fts DESYNC (rows={total}, fts_idx={c2_idx}) {DASH} run memory_db.py init"
            )
        score += c2
        maxpts += 20
        out.append(f"| 2 | FTS index cohesion | {c2}/20 | {c2_detail} |")

        # Criterio 3: coppie near-dup (saltato con --quick).
        if mode == "quick":
            c3, c3_detail = 20, f"skipped (--quick) {DASH} awarded full"
        elif total == 0:
            c3, c3_detail = 20, "no rows to compare (full)"
        else:
            texts = [r[0] for r in scan.q(conn, "SELECT text FROM memories ORDER BY id")]
            dups = count_near_dup_pairs(texts, dup_sim_min)
            # Ogni coppia implica circa una riga ridondante; limite al totale.
            c3 = pct_points(total - min(dups, total), total, 20)
            c3_detail = (
                f"{dups} near-dup pair(s) (Jaccard>={dup_raw}) across {total} rows"
            )
        score += c3
        maxpts += 20
        out.append(f"| 3 | No near-dup rows | {c3}/20 | {c3_detail} |")

        # Criterio 4: presenza della tabella virtuale vec0.
        c4_vec = scan.scalar(
            conn, "SELECT COUNT(*) FROM sqlite_master WHERE name='memories_vec'"
        )
        if c4_vec >= 1:
            c4 = 15
            c4_detail = "memories_vec present (vec0); full triple via memory_db.py stats"
        else:
            c4 = 0
            c4_detail = (
                f"memories_vec MISSING {DASH} vector search unavailable; "
                "rebuild via memory_db.py init"
            )
        score += c4
        maxpts += 15
        out.append(f"| 4 | vec store soundness | {c4}/15 | {c4_detail} |")

        # Criterio 5: metadati completi.
        c5_bad = scan.scalar(
            conn,
            "SELECT COUNT(*) FROM memories WHERE name IS NULL OR TRIM(name)='' "
            "OR description IS NULL OR TRIM(description)='' "
            "OR type IS NULL OR TRIM(type)=''",
        )
        if total == 0:
            c5, c5_detail = 0, "no rows"
        else:
            c5 = pct_points(total - c5_bad, total, 15)
            c5_detail = f"{total - c5_bad}/{total} have name+description+type"
        score += c5
        maxpts += 15
        out.append(f"| 5 | Metadata complete | {c5}/15 | {c5_detail} |")

        # Criterio 6: staleness, N/A se l'edit-span del corpus e' sotto STALE_DAYS
        # (in quel caso non entra ne' nel numeratore ne' in MAXPTS).
        c6_na = False
        c6_stale = 0
        span_rows = scan.q(
            conn,
            "SELECT COALESCE(julianday(MAX(updated)) - julianday(MIN(updated)), 0) "
            "FROM memories",
        )
        try:
            span = float(span_rows[0][0])
        except (IndexError, TypeError, ValueError):
            span = 0.0
        if total == 0:
            maxpts += 10
            out.append("| 6 | Staleness managed | 0/10 | no rows |")
        elif span < stale_days:
            c6_na = True
            out.append(
                f"| 6 | Staleness managed | N/A | edit-span {span:.1f}d < {stale_days}d "
                f"{DASH} unmeasurable until timestamps diverge |"
            )
        else:
            c6_stale = scan.scalar(
                conn,
                "SELECT COUNT(*) FROM memories WHERE updated < datetime(?, ?)",
                (scan.now_expr(), f"-{stale_days} days"),
            )
            c6 = pct_points(total - c6_stale, total, 10)
            score += c6
            maxpts += 10
            out.append(
                f"| 6 | Staleness managed | {c6}/10 | {c6_stale}/{total} older than "
                f"{stale_days}d (edit-span {span:.1f}d) |"
            )

        total_bytes = scan.scalar(
            conn, "SELECT COALESCE(SUM(LENGTH(text)),0) FROM memories"
        )
    finally:
        conn.close()

    if mode == "verbose":
        out += ["", "### Per-tier DB signals (from scan_mem_matrix.py)", "```"]
        out += scan.table_lines(path)
        out.append("```")

    # Trigger v3: solo informativi, non toccano il punteggio.
    out += ["", f"### v3 Triggers (informational {DASH} not scored)"]
    fired = 0
    if total_bytes > corpus_max:
        out.append(f"- Corpus {total_bytes}B (>{corpus_max}B): run /lt-mem --quick all")
        fired += 1
    if c1_over > 0:
        out.append(
            f"- {c1_over} oversized row(s) (>{oversize}B): consolidate via "
            f"/lt-mem --compact ({c1_exempt} exempt, skipped)"
        )
        fired += 1
    if total > 0 and c2_idx != total:
        out.append(
            f"- FTS index desync (rows={total}, fts_idx={c2_idx}): "
            "rebuild via memory_db.py init"
        )
        fired += 1
    if total > 0 and not c6_na:
        stale_pct = c6_stale * 100 // total
        if stale_pct > 10:
            out.append(
                f"- {stale_pct}% rows stale (>{stale_days}d): run /lt-mem archive pass"
            )
            fired += 1
    if fired == 0:
        out.append("v3 Triggers: None. DB within thresholds.")

    # Rinormalizzazione su MAXPTS con arrotondamento half-up, poi tetto quando
    # un criterio e' N/A (valutazione incompleta: niente 100 certificato).
    final = (2 * score * 100 + maxpts) // (2 * maxpts) if maxpts > 0 else 0
    final = max(0, min(100, final))
    if maxpts < 100 and final > incomplete_cap:
        final = incomplete_cap
    basis = ""
    if maxpts < 100:
        basis = (
            f"; {100 - maxpts} pts N/A excluded; capped at {incomplete_cap} "
            f"{DASH} incomplete assessment"
        )
    out += ["", f"Scored {score}/{maxpts} pts (renormalized to /100{basis}).", f"SCORE: {final}/100"]
    return out


def main(argv: list[str]) -> int:
    scan._unix_newlines()
    mode = "normal"
    for arg in argv:
        if arg == "--quick":
            mode = "quick"
        elif arg == "--verbose":
            mode = "verbose"
    try:
        lines = report(mode)
    except CrossProfileDBError as exc:
        # Unica eccezione al "mai exit != 0": il muro tra profili fallisce chiuso
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # strumento di report: mai un crash, mai exit != 0
        lines = [f"mem_health internal error: {exc}", "SCORE: 0/100"]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
