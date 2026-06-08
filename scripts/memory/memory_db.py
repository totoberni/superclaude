"""v3 Memory subsystem — hybrid-search SQLite DB.

Hybrid keyword (FTS5 BM25) + semantic (sqlite-vec KNN) search over the curated
agent-memory corpus. Flat MD remains the SOT/backup until a gated cutover; this
DB is strictly additive and non-destructive (rollback = delete the .db file).

Always run under ~/.claude/.venv/bin/python (fastembed + sqlite-vec live there).

Spec: ~/.claude/plans/superclaude-v3/memory-schema.md (§Schema, §Python interfaces,
§Migration rules). Public API: init_db, upsert, search, get_html, migrate.
"""

from __future__ import annotations

import hashlib
import html as _html
import re
import sqlite3
from pathlib import Path
from typing import Iterable

import sqlite_vec

# --- Constants ---------------------------------------------------------------

DB_PATH = Path.home() / ".claude" / "agent-memory" / ".memory.db"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
# Pinned, persistent model cache. The FastEmbed default ($TMPDIR/fastembed_cache)
# is ephemeral AND differs per shell/sandbox, causing "model not found" across
# contexts (warm-up shell vs hook/skill runtime). Pin it so it is found everywhere.
EMBED_CACHE_DIR = Path.home() / ".claude" / ".cache" / "fastembed"
RRF_K = 60  # Reciprocal Rank Fusion constant (standard default)
ARCHIVE_TIER = "archive"  # Re-tiered, out-of-active-recall memories (still queryable).

# Migration walk rules (spec §Migration rules).
_EXCLUDE_PARTS = {"_system", "_archive", "archive", "_compact-snapshots", "_stop-snapshots"}

# --- Embedder (lazy singleton) ----------------------------------------------

_embedder = None


def _get_embedder():
    """Load the FastEmbed bge-small model once.

    Uses a PINNED persistent cache (EMBED_CACHE_DIR) so the model is found
    consistently across shells/sandboxes. First use downloads ~130MB; once
    cached there, no network is needed.
    """
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        EMBED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _embedder = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(EMBED_CACHE_DIR))
    return _embedder


def _embed(text: str) -> list[float]:
    """Embed one document → 384-dim float list."""
    vec = next(iter(_get_embedder().embed([text])))
    return [float(x) for x in vec]


# --- Connection --------------------------------------------------------------


def _connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open a connection with the sqlite-vec extension loaded."""
    conn = sqlite3.connect(str(db_path))
    # Wait up to 5s for a competing writer instead of failing immediately with
    # "database is locked" — matters when a cross-device sync writes this DB
    # concurrently with a local agent's write (Phase EM concurrency hardening).
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


# --- Schema ------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE,
  tier TEXT,
  agent TEXT,
  type TEXT,
  name TEXT,
  description TEXT,
  text TEXT NOT NULL,
  html TEXT,
  created TEXT,
  updated TEXT,
  hash TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  name, description, text,
  content='memories', content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(embedding float[384]);

CREATE TABLE IF NOT EXISTS links (src_id INTEGER, dst_name TEXT);
"""

# External-content FTS sync triggers (keep FTS shadow in lockstep with memories).
_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, name, description, text)
  VALUES (new.id, new.name, new.description, new.text);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, name, description, text)
  VALUES ('delete', old.id, old.name, old.description, old.text);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, name, description, text)
  VALUES ('delete', old.id, old.name, old.description, old.text);
  INSERT INTO memories_fts(rowid, name, description, text)
  VALUES (new.id, new.name, new.description, new.text);
END;
"""


def init_db(db_path: Path | str = DB_PATH) -> None:
    """Create schema + FTS sync triggers. Idempotent."""
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.executescript(_TRIGGERS)
        conn.commit()
    finally:
        conn.close()


# --- Hash --------------------------------------------------------------------


def _content_hash(text: str, name: str, description: str) -> str:
    """Stable content hash for idempotent migration + render-cache keying."""
    h = hashlib.sha256()
    h.update((name or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((description or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((text or "").encode("utf-8"))
    return h.hexdigest()


# --- Wikilink extraction -----------------------------------------------------

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _extract_links(text: str) -> list[str]:
    """Return distinct [[wikilink]] target names, preserving first-seen order."""
    seen: dict[str, None] = {}
    for m in _WIKILINK_RE.finditer(text or ""):
        seen.setdefault(m.group(1).strip(), None)
    return list(seen)


# --- Upsert ------------------------------------------------------------------


def upsert(
    path: str,
    tier: str,
    agent: str | None,
    type: str,
    name: str,
    description: str,
    text: str,
    db_path: Path | str = DB_PATH,
) -> int:
    """Insert or update a memory keyed by `path`.

    Computes a content hash; if the row is new or its content changed, the text
    is (re-)embedded into memories_vec and links are refreshed. html is left
    NULL (rendered lazily by get_html). Returns the row id.
    """
    path = str(path)
    new_hash = _content_hash(text, name, description)
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT id, hash FROM memories WHERE path = ?", (path,)
        )
        existing = cur.fetchone()
        now = _now()

        if existing is None:
            cur = conn.execute(
                """INSERT INTO memories
                   (path, tier, agent, type, name, description, text, html, created, updated, hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                (path, tier, agent, type, name, description, text, now, now, new_hash),
            )
            row_id = cur.lastrowid
            _refresh_embedding(conn, row_id, text)
            _refresh_links(conn, row_id, text)
        else:
            row_id = existing["id"]
            if existing["hash"] != new_hash:
                # Content changed: update metadata + body, drop stale html, re-embed.
                conn.execute(
                    """UPDATE memories
                       SET tier=?, agent=?, type=?, name=?, description=?, text=?,
                           html=NULL, updated=?, hash=?
                       WHERE id=?""",
                    (tier, agent, type, name, description, text, now, new_hash, row_id),
                )
                _refresh_embedding(conn, row_id, text)
                _refresh_links(conn, row_id, text)
            else:
                # Unchanged content; refresh metadata only (cheap, no re-embed).
                conn.execute(
                    """UPDATE memories
                       SET tier=?, agent=?, type=?, name=?, description=?, updated=?
                       WHERE id=?""",
                    (tier, agent, type, name, description, now, row_id),
                )
        conn.commit()
        return row_id
    finally:
        conn.close()


def _refresh_embedding(conn: sqlite3.Connection, row_id: int, text: str) -> None:
    """(Re)embed `text` and store in memories_vec keyed by rowid == memories.id."""
    embedding = _embed(text)
    conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (row_id,))
    conn.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
        (row_id, sqlite_vec.serialize_float32(embedding)),
    )


def _refresh_links(conn: sqlite3.Connection, row_id: int, text: str) -> None:
    """Replace the link edges originating from this row."""
    conn.execute("DELETE FROM links WHERE src_id = ?", (row_id,))
    for dst in _extract_links(text):
        conn.execute(
            "INSERT INTO links(src_id, dst_name) VALUES (?, ?)", (row_id, dst)
        )


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --- Search ------------------------------------------------------------------


def search(
    query: str,
    k: int = 8,
    mode: str = "hybrid",
    db_path: Path | str = DB_PATH,
    include_archived: bool = False,
    tier: str | None = None,
) -> list[dict]:
    """Search memories. mode ∈ {'fts', 'vec', 'hybrid'}.

    - fts: FTS5 BM25 ranking over name/description/text.
    - vec: sqlite-vec KNN over the bge-small embedding of `query`.
    - hybrid: Reciprocal Rank Fusion of the fts and vec ranked lists.

    Returns up to `k` rows as dicts (the full memories row, text included),
    each with an added float `score` (RRF score for hybrid, raw rank/distance
    proxies for the single-mode paths).

    Archive-tier rows are EXCLUDED from the active recall horizon unless
    `include_archived=True` (CLI: `--all`) OR an explicit `tier='archive'` filter
    (CLI: `--tier archive`) is given. The exclusion is a post-filter on the ranked
    rowids — each arm over-fetches so dropping archived hits does not starve `k`.
    """
    # tier='archive' implies the caller wants archived rows; never exclude then.
    exclude_archive = not include_archived and tier != ARCHIVE_TIER
    # Over-fetch when filtering so the post-filter still yields up to `k`.
    fetch_k = max(k * 4, k) if (exclude_archive or tier is not None) else k
    conn = _connect(db_path)
    try:
        if mode == "fts":
            ranked = _fts_ranked(conn, query, fetch_k)
            scored = [(rid, -bm25) for rid, bm25 in ranked]
        elif mode == "vec":
            ranked = _vec_ranked(conn, query, fetch_k)
            # Lower distance = better; expose as score = -distance.
            scored = [(rid, -dist) for rid, dist in ranked]
        elif mode == "hybrid":
            scored = _hybrid_scored(conn, query, fetch_k)
        else:
            raise ValueError(f"unknown mode: {mode!r} (expected fts|vec|hybrid)")
        scored = _apply_tier_filter(conn, scored, exclude_archive, tier)[:k]
        return _materialize(conn, scored)
    finally:
        conn.close()


def _apply_tier_filter(
    conn: sqlite3.Connection,
    scored: list[tuple[int, float]],
    exclude_archive: bool,
    tier: str | None,
) -> list[tuple[int, float]]:
    """Drop ranked (rowid, score) pairs failing the archive-exclusion/tier filter.

    Preserves rank order. A single lookup of the candidate rowids' tiers avoids
    re-querying per row. No-op when neither constraint is active.
    """
    if not exclude_archive and tier is None:
        return scored
    ids = [rid for rid, _ in scored]
    if not ids:
        return scored
    placeholders = ",".join("?" * len(ids))
    tier_by_id = {
        r["id"]: r["tier"]
        for r in conn.execute(
            f"SELECT id, tier FROM memories WHERE id IN ({placeholders})", ids
        ).fetchall()
    }
    out: list[tuple[int, float]] = []
    for rid, score in scored:
        row_tier = tier_by_id.get(rid)
        if exclude_archive and row_tier == ARCHIVE_TIER:
            continue
        if tier is not None and row_tier != tier:
            continue
        out.append((rid, score))
    return out


def _fts_ranked(conn: sqlite3.Connection, query: str, k: int) -> list[tuple[int, float]]:
    """Return [(rowid, bm25)] best-first. bm25() is ascending (lower = better)."""
    fts_query = _fts_match_query(query)
    if not fts_query:
        return []
    rows = conn.execute(
        """SELECT rowid, bm25(memories_fts) AS score
           FROM memories_fts
           WHERE memories_fts MATCH ?
           ORDER BY score
           LIMIT ?""",
        (fts_query, k),
    ).fetchall()
    return [(r["rowid"], r["score"]) for r in rows]


def _vec_ranked(conn: sqlite3.Connection, query: str, k: int) -> list[tuple[int, float]]:
    """Return [(rowid, distance)] nearest-first via sqlite-vec KNN."""
    q = sqlite_vec.serialize_float32(_embed(query))
    rows = conn.execute(
        """SELECT rowid, distance
           FROM memories_vec
           WHERE embedding MATCH ? AND k = ?
           ORDER BY distance""",
        (q, k),
    ).fetchall()
    return [(r["rowid"], r["distance"]) for r in rows]


def _hybrid_scored(
    conn: sqlite3.Connection, query: str, k: int
) -> list[tuple[int, float]]:
    """Fuse FTS and vec ranked lists with Reciprocal Rank Fusion → (rowid, score).

    Each list contributes 1/(RRF_K + rank) per document; documents are sorted by
    summed contribution. Over-fetch each arm (k*4) so fusion has signal beyond
    the final cut. Returns the fused (rowid, RRF-score) list (best-first, capped
    at k) without materializing — so callers can post-filter on rowid metadata.
    """
    fetch = max(k * 4, k)
    fts = _fts_ranked(conn, query, fetch)
    vec = _vec_ranked(conn, query, fetch)

    scores: dict[int, float] = {}
    for rank, (rid, _) in enumerate(fts):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, (rid, _) in enumerate(vec):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank)

    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:k]


def _hybrid(conn: sqlite3.Connection, query: str, k: int) -> list[dict]:
    """Materialized RRF fusion (back-compat wrapper over `_hybrid_scored`)."""
    return _materialize(conn, _hybrid_scored(conn, query, k))


def _materialize(
    conn: sqlite3.Connection, scored: list[tuple[int, float]]
) -> list[dict]:
    """Hydrate (rowid, score) pairs into full memory dicts, preserving order."""
    out: list[dict] = []
    for rid, score in scored:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (rid,)).fetchone()
        if row is None:
            continue
        d = dict(row)
        d["score"] = score
        out.append(d)
    return out


# FTS5 reserved characters / operators that break a bare MATCH if passed raw.
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _fts_match_query(query: str) -> str:
    """Build a safe FTS5 MATCH string: OR-join quoted alphanumeric tokens.

    Quoting each token sidesteps FTS5 syntax errors from punctuation/operators
    in free-text queries, while OR keeps recall high for the fusion arm.
    """
    tokens = _FTS_TOKEN_RE.findall(query or "")
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


# --- HTML render (lazy) ------------------------------------------------------


def get_html(id: int, db_path: Path | str = DB_PATH) -> str | None:
    """Return cached html for a memory; render + cache from text on first call.

    Rendering delegates to a sibling render.py (`render(text, kind)`). If that
    module is unavailable, fall back to an escaped <pre> block so the viewer
    never breaks.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, text, html FROM memories WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            return None
        if row["html"] is not None:
            return row["html"]

        rendered = _render_text(row["text"])
        conn.execute(
            "UPDATE memories SET html = ? WHERE id = ?", (rendered, id)
        )
        conn.commit()
        return rendered
    finally:
        conn.close()


# A memory's `text` is markdown that may EMBED fenced notation blocks. We detect
# ```mermaid / ```dot / ```vega / ```tikz (and the ```{tikz} brace form), render
# each via render.render(block, kind), render the surrounding markdown as its own
# segment, and concatenate. render() is called WITHOUT a ScriptRegistry so each
# cached fragment is self-contained (every CDN script it needs is emitted inline)
# — matching render.py's per-row contract used by the lazy get_html cache.
_NOTATION_KINDS = {"mermaid", "dot", "vega", "tikz"}

# Fenced block opener: ``` (or more), optional `{`, an info word, optional `}`,
# to end of line; capture the kind. Intra-line spacing MUST be [ \t]* not \s* —
# \s* matches newlines, so a bare ```mermaid opener would spill onto the next line
# and swallow the first body line (e.g. a 'graph LR' header). Closing fence = >=3 backticks.
_FENCE_RE = re.compile(
    r"^(?P<fence>`{3,})[ \t]*\{?[ \t]*(?P<kind>[A-Za-z][\w+-]*)[ \t]*\}?[^\n]*\n"
    r"(?P<body>.*?)"
    r"^(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def _render_text(text: str, kind: str = "markdown") -> str:
    """Render a memory's `text` to one self-contained HTML fragment.

    The `text` is markdown that may embed fenced notation blocks (```mermaid,
    ```dot, ```vega, ```tikz / ```{tikz}). Each such block is rendered through
    render.render(block, kind); the prose between/around blocks is rendered as
    markdown. Segments are concatenated in source order. `kind` selects the
    renderer for the whole `text` when it is not markdown (callers pass the
    default; the segmentation only applies to the markdown path).

    Falls back to an escaped <pre> for the entire body if render.py cannot be
    imported, so the viewer never breaks.
    """
    render_fn = _load_render()
    if render_fn is None:
        return _pre_fallback(text)

    text = text or ""
    if kind != "markdown":
        # Non-markdown bodies are a single notation of the given kind.
        return render_fn(text, kind)

    parts: list[str] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        block_kind = m.group("kind").lower()
        if block_kind not in _NOTATION_KINDS:
            # Not a notation we render server/client-side (e.g. ```python) —
            # leave it in the markdown stream so marked.js renders it as code.
            continue
        prose = text[pos:m.start()]
        if prose.strip():
            parts.append(render_fn(prose, "markdown"))
        parts.append(render_fn(m.group("body"), block_kind))
        pos = m.end()

    tail = text[pos:]
    if not parts:
        # No embedded notation: render the whole body as markdown (cheapest path).
        return render_fn(text, "markdown")
    if tail.strip():
        parts.append(render_fn(tail, "markdown"))
    return "".join(parts)


def _load_render():
    """Import the sibling render.py's `render` callable, or None on failure."""
    try:
        import importlib
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        return importlib.import_module("render").render
    except Exception:
        return None


def _pre_fallback(text: str) -> str:
    return "<pre>" + _html.escape(text or "") + "</pre>"


# --- Migration ---------------------------------------------------------------


def migrate(root: Path | str, db_path: Path | str = DB_PATH) -> dict:
    """Walk the curated agent-memory corpus under `root` and upsert each file.

    Non-destructive: source MD is never modified. Idempotent via content hash.
    Returns a summary {'upserted': n, 'skipped': n, 'files': [paths]}.

    INCLUDE: instance/<agent>/*.md, shared/projects/**.md (incl _complete/),
             shared/global/*.md, class/*/*.md
    EXCLUDE: _system/**, **/_archive/**, **/archive/**, _compact-snapshots,
             _stop-snapshots
    """
    root = Path(root)
    files = sorted(_iter_curated(root))
    upserted = 0
    for fp in files:
        meta = _parse_file(fp, root)
        upsert(
            path=str(fp),
            tier=meta["tier"],
            agent=meta["agent"],
            type=meta["type"],
            name=meta["name"],
            description=meta["description"],
            text=meta["text"],
            db_path=db_path,
        )
        upserted += 1
    return {"upserted": upserted, "files": [str(f) for f in files]}


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    return any(part in _EXCLUDE_PARTS for part in rel_parts)


def _iter_curated(root: Path) -> Iterable[Path]:
    """Yield curated .md files honoring INCLUDE/EXCLUDE rules.

    Symlinked tier roots (e.g. _archive -> _system/_archive) are skipped by the
    exclusion check on path parts; `rglob` does not follow directory symlinks by
    default, so symlinked dirs are not descended into.
    """
    include_globs = [
        ("instance", "*/*.md"),
        ("shared/projects", "**/*.md"),
        ("shared/global", "*.md"),
        ("class", "*/*.md"),
    ]
    seen: set[Path] = set()
    for subdir, pattern in include_globs:
        base = root / subdir
        if not base.exists():
            continue
        for fp in base.glob(pattern):
            if not fp.is_file():
                continue
            resolved = fp.resolve()
            if resolved in seen:
                continue
            rel = fp.relative_to(root)
            if _is_excluded(rel.parts):
                continue
            seen.add(resolved)
            yield fp


# --- Frontmatter / metadata derivation ---------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_VALID_TYPES = {"feedback", "project", "reference", "user", "index"}


def _parse_file(fp: Path, root: Path) -> dict:
    """Derive (tier, agent, type, name, description, text) for one file.

    Frontmattered files: pull name/description/type from YAML; text = body.
    Non-frontmattered files (e.g. prose project memories): derive name from the
    filename, type from tier (or 'index' for MEMORY.md), description from the
    first markdown heading. text = full raw content.
    """
    raw = fp.read_text(encoding="utf-8", errors="replace")
    rel = fp.relative_to(root)
    tier, agent = _classify_tier(rel)

    fm, body = _split_frontmatter(raw)
    is_index = fp.name == "MEMORY.md"

    if fm:
        name = fm.get("name") or fp.stem
        description = fm.get("description") or _first_heading(body)
        ftype = fm.get("type")
        if is_index:
            ftype = "index"
        if ftype not in _VALID_TYPES:
            ftype = "index" if is_index else _default_type(tier)
        text = body if body.strip() else raw
    else:
        name = fp.stem
        description = _first_heading(raw)
        ftype = "index" if is_index else _default_type(tier)
        text = raw

    return {
        "tier": tier,
        "agent": agent,
        "type": ftype,
        "name": name,
        "description": description,
        "text": text,
    }


def _classify_tier(rel: Path) -> tuple[str, str | None]:
    """Map a relative path to (tier, agent). agent is set only for instance/class."""
    parts = rel.parts
    head = parts[0] if parts else ""
    if head == "instance":
        return "instance", (parts[1] if len(parts) > 1 else None)
    if head == "class":
        return "class", (parts[1] if len(parts) > 1 else None)
    if head == "shared":
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "projects":
            return "shared-projects", None
        if sub == "global":
            return "shared-global", None
        return "shared", None
    return head or "unknown", None


def _default_type(tier: str) -> str:
    """Fallback type when no frontmatter declares one."""
    if tier == "shared-projects":
        return "project"
    if tier == "shared-global":
        return "reference"
    return "project"


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Split a leading YAML frontmatter block. Returns ({} , raw) if absent."""
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    fm = _parse_simple_yaml(m.group(1))
    body = raw[m.end():]
    return fm, body


def _parse_simple_yaml(block: str) -> dict:
    """Minimal flat `key: value` YAML reader (no external dep).

    Handles the only frontmatter shape used in this corpus: top-level scalar
    keys (name, description, type) plus an ignored nested `metadata:` mapping.
    """
    out: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":  # nested (e.g. under metadata:) — skip
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if val:  # skip mapping headers like `metadata:`
            out[key] = val
    return out


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _first_heading(text: str) -> str:
    """First markdown heading text, else first non-empty line, truncated."""
    m = _HEADING_RE.search(text or "")
    if m:
        return m.group(1).strip()[:200]
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()[:200]
    return ""


# --- CLI helpers -------------------------------------------------------------


def _db_get_by_id(conn: sqlite3.Connection, row_id: int):
    return conn.execute("SELECT * FROM memories WHERE id = ?", (row_id,)).fetchone()


def _db_get_by_name(conn: sqlite3.Connection, name: str):
    return conn.execute(
        "SELECT * FROM memories WHERE name = ?", (name,)
    ).fetchone()


def _db_get_by_path(conn: sqlite3.Connection, path: str):
    # `path` is the UNIQUE identity column — unambiguous, unlike `name`
    # (names can collide across tiers). Cross-device sync must address rows
    # by path so a delete never targets the wrong row.
    return conn.execute(
        "SELECT * FROM memories WHERE path = ?", (path,)
    ).fetchone()


def _prune_row(row_id: int, db_path: Path | str = DB_PATH) -> None:
    """Delete a memory + its FTS/vec rows. Raises LookupError if not found."""
    conn = _connect(db_path)
    try:
        row = _db_get_by_id(conn, row_id)
        if row is None:
            raise LookupError(f"no memory with id={row_id}")
        conn.execute("DELETE FROM memories WHERE id = ?", (row_id,))
        conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (row_id,))
        conn.execute("DELETE FROM links WHERE src_id = ?", (row_id,))
        conn.commit()
    finally:
        conn.close()


# --- Similarity (hybrid cosine + token-Jaccard) ------------------------------

# Hybrid-similarity weighting: lexical Jaccard 0.3, semantic cosine 0.7. Cosine
# dominates so semantically-related-but-differently-worded memories still rank;
# Jaccard breaks ties toward shared vocabulary (near-duplicate detection).
_SIM_JACCARD_W = 0.3
_SIM_COSINE_W = 0.7

# Token splitter for Jaccard: lowercase, split on any non-alphanumeric run, drop
# empties → a SET of word tokens (order/count-insensitive, matching set-Jaccard).
_JACCARD_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _jaccard_tokens(text: str) -> set[str]:
    """Lowercase + split on non-alphanumeric → token SET (for set-Jaccard)."""
    return {t for t in _JACCARD_TOKEN_RE.split((text or "").lower()) if t}


def _token_jaccard(a: str, b: str) -> float:
    """Token-set Jaccard |A∩B|/|A∪B| of two texts; 0.0 if the union is empty."""
    sa, sb = _jaccard_tokens(a), _jaccard_tokens(b)
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def _stored_vector(conn: sqlite3.Connection, row_id: int):
    """Read a row's PRECOMPUTED embedding back from memories_vec as a numpy array.

    Honors the "no model reload" constraint: the embedder is NEVER invoked for an
    existing row — the stored float[384] vector is deserialized via vec_to_json.
    Returns a 1-D float32 numpy array, or None if the row has no vec entry.
    """
    import json

    import numpy as np

    row = conn.execute(
        "SELECT vec_to_json(embedding) AS j FROM memories_vec WHERE rowid = ?",
        (row_id,),
    ).fetchone()
    if row is None or row["j"] is None:
        return None
    return np.asarray(json.loads(row["j"]), dtype="float32")


def _cosine(a, b) -> float:
    """Exact cosine of two vectors via numpy dot / (||a||·||b||); 0.0 if degenerate.

    bge-small vectors are ~unit-norm, but the full normalization is computed to
    stay correct for any (e.g. future non-normalized) embedding.
    """
    import numpy as np

    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def similar(
    row_id: int,
    k: int = 8,
    tier: str | None = None,
    db_path: Path | str = DB_PATH,
) -> list[dict]:
    """Hybrid-rank corpus memories most similar to the memory `row_id`.

    Uses the target's PRECOMPUTED stored embedding (no re-embed). Candidate set is
    a FULL SCAN over all stored vectors — the corpus is small (~135 rows) so this
    is the simplest correct approach (KISS); no KNN prefilter is needed and a full
    scan never silently drops a semantically-near row that a small KNN pool would
    miss. For each candidate (excluding the target, and any archive-tier row, and
    honoring an optional `--tier` filter):

      cosine   = exact cosine of the two stored float32 vectors
      jaccard  = token-set Jaccard of the two `text` bodies
      combined = 0.3*jaccard + 0.7*cosine

    Returns up to `k` dicts (full memory row + cosine/jaccard/combined floats),
    sorted by `combined` descending. Raises LookupError if `row_id` is absent or
    has no stored embedding.
    """
    conn = _connect(db_path)
    try:
        target = _db_get_by_id(conn, row_id)
        if target is None:
            raise LookupError(f"no memory with id={row_id}")
        tvec = _stored_vector(conn, row_id)
        if tvec is None:
            raise LookupError(f"memory id={row_id} has no stored embedding")
        ttext = target["text"] or ""

        rows = conn.execute("SELECT * FROM memories").fetchall()
        scored: list[dict] = []
        for r in rows:
            if r["id"] == row_id:
                continue
            # Archived rows are outside the active recall horizon; never surface
            # them unless the caller explicitly asks for that tier.
            if r["tier"] == ARCHIVE_TIER and tier != ARCHIVE_TIER:
                continue
            if tier is not None and r["tier"] != tier:
                continue
            cvec = _stored_vector(conn, r["id"])
            if cvec is None:
                continue
            cos = _cosine(tvec, cvec)
            jac = _token_jaccard(ttext, r["text"] or "")
            d = dict(r)
            d["cosine"] = cos
            d["jaccard"] = jac
            d["combined"] = _SIM_COSINE_W * cos + _SIM_JACCARD_W * jac
            scored.append(d)

        scored.sort(key=lambda d: d["combined"], reverse=True)
        return scored[:k]
    finally:
        conn.close()


# --- Archive (re-tier in place, NOT prune) -----------------------------------


# Archived rows encode their ORIGINAL (tier, path) inside the new path so the
# round-trip is exactly lossless with no extra column:
#   archive/<orig-tier>::<orig-path>
# Putting the row "under archive/" keeps path↔tier consistent (leading segment
# is the archive tier) and preserves the full original path tail verbatim, so it
# round-trips for absolute, nested, or synthetic (no-suffix) paths alike. The
# '::' separator delimits the recoverable original tier from the original path
# (paths never contain '::'). Uniqueness is inherited from the original UNIQUE path.
_ARCH_SEP = "::"


def _archived_path(path: str, tier: str) -> str:
    """Compute the archive-tier path for an active row (losslessly reversible)."""
    return f"{ARCHIVE_TIER}/{tier}{_ARCH_SEP}{path}"


def _unarchived(path: str) -> tuple[str, str]:
    """Reverse `_archived_path`: recover the EXACT original (tier, path).

    Expects ``archive/<orig-tier>::<orig-path>``. Falls back to a sensible
    ``shared-projects`` home keyed on the basename if the path is not in the
    expected archive shape (e.g. a row archived by some other tool).
    """
    prefix = f"{ARCHIVE_TIER}/"
    if path and path.startswith(prefix) and _ARCH_SEP in path:
        rest = path[len(prefix):]
        orig_tier, _, orig_path = rest.partition(_ARCH_SEP)
        return orig_tier, orig_path
    tail = path.rsplit("/", 1)[-1] if path else path
    return "shared-projects", f"shared-projects/{tail}"


def _retiered_path(path: str, target_tier: str) -> str:
    """Compute the new path for a DELIBERATE in-place re-tier to ``target_tier``.

    The new path is ``<target_tier>/<tail>`` where ``<tail>`` is the old path
    with its FIRST segment (the encoded source tier) stripped — keeping the
    leading path segment consistent with the row's tier (the same invariant the
    default upsert path ``<tier>/<agent-or-_>/<name>.md`` relies on). Unlike
    `_archived_path`, this is NOT origin-encoded: `retier` is for forward,
    intentional moves (e.g. instance → shared-global), not reversible tombstoning.

    A path with no '/' (or an empty path) has no leading segment to strip, so the
    whole path is treated as the tail.
    """
    tail = path.split("/", 1)[1] if path and "/" in path else (path or "")
    return f"{target_tier}/{tail}"


def _retier_update(
    conn: sqlite3.Connection, row_id: int, new_tier: str, new_path: str
) -> None:
    """Shared in-place re-tier core for `archive` and `retier` (DRY).

    Performs ONLY the `tier`/`path`/`updated` UPDATE on an existing row. The
    `text`/`html`/embedding/FTS shadows are deliberately untouched: tier & path
    are not in the FTS index, and the vec row is keyed by rowid — so this neither
    re-embeds, desyncs the FTS, nor creates a duplicate row. The caller owns the
    transaction boundary (commit/rollback) and any precondition checks.
    """
    conn.execute(
        "UPDATE memories SET tier = ?, path = ?, updated = ? WHERE id = ?",
        (new_tier, new_path, _now(), row_id),
    )


def archive(
    row_id: int, unarchive: bool = False, db_path: Path | str = DB_PATH
) -> dict:
    """Re-tier a memory in place — preserving the row AND its embedding.

    NO dup, NO re-embed, NO delete. Only `tier` and `path` change via a direct
    UPDATE; `text`/`html`/embedding/FTS are untouched (tier & path are not in the
    FTS index, and the vec row is keyed by rowid → no FTS desync, no re-embed).

    archive:   tier → 'archive', path → archive/<orig-tier>/<name>.
    unarchive: tier → restored original, path → <orig-tier>/<name>.

    Returns {'id', 'name', 'old_tier', 'new_tier', 'old_path', 'new_path'}.
    Raises LookupError if absent; ValueError on a redundant (un)archive.
    """
    conn = _connect(db_path)
    try:
        row = _db_get_by_id(conn, row_id)
        if row is None:
            raise LookupError(f"no memory with id={row_id}")
        old_tier = row["tier"]
        old_path = row["path"]

        if unarchive:
            if old_tier != ARCHIVE_TIER:
                raise ValueError(
                    f"memory id={row_id} is not archived (tier={old_tier!r})"
                )
            new_tier, new_path = _unarchived(old_path or "")
        else:
            if old_tier == ARCHIVE_TIER:
                raise ValueError(f"memory id={row_id} is already archived")
            new_tier = ARCHIVE_TIER
            new_path = _archived_path(old_path or "", old_tier or "unknown")

        _retier_update(conn, row_id, new_tier, new_path)
        conn.commit()
        return {
            "id": row_id,
            "name": row["name"],
            "old_tier": old_tier,
            "new_tier": new_tier,
            "old_path": old_path,
            "new_path": new_path,
        }
    finally:
        conn.close()


def retier(
    row_id: int, target_tier: str, db_path: Path | str = DB_PATH
) -> dict:
    """Deliberately re-tier a memory in place to ``target_tier``.

    General-purpose forward re-tier for INTENTIONAL moves (e.g. promotion
    instance → shared-global). Like `archive`, this preserves the row AND its
    embedding (no dup, no re-embed, no delete) via the shared `_retier_update`
    core — only `tier`/`path` change.

    New path = ``<target_tier>/<tail>`` (see `_retiered_path`): the old path's
    first tier segment is stripped and replaced with ``target_tier``.

    `archive` is kept SEPARATE: it needs the origin-encoded reversible path for a
    lossless `--unarchive` round-trip, whereas this is a one-way intentional move.
    Re-tiering TO 'archive' is therefore rejected — use the `archive` command.

    Returns {'id', 'name', 'old_tier', 'new_tier', 'old_path', 'new_path'}.
    Raises LookupError if absent; ValueError on archive-target or a redundant move.
    """
    if target_tier == ARCHIVE_TIER:
        raise ValueError(
            "use the `archive` command to re-tier to 'archive' "
            "(it encodes the origin path for a lossless --unarchive)"
        )
    conn = _connect(db_path)
    try:
        row = _db_get_by_id(conn, row_id)
        if row is None:
            raise LookupError(f"no memory with id={row_id}")
        old_tier = row["tier"]
        old_path = row["path"]
        if old_tier == target_tier:
            raise ValueError(
                f"memory id={row_id} is already in tier {target_tier!r}"
            )

        new_path = _retiered_path(old_path or "", target_tier)
        _retier_update(conn, row_id, target_tier, new_path)
        conn.commit()
        return {
            "id": row_id,
            "name": row["name"],
            "old_tier": old_tier,
            "new_tier": target_tier,
            "old_path": old_path,
            "new_path": new_path,
        }
    finally:
        conn.close()


# --- CLI entry ---------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json
    import sys

    def _die(msg: str, code: int = 1) -> None:
        print(f"error: {msg}", file=sys.stderr)
        sys.exit(code)

    parser = argparse.ArgumentParser(
        prog="memory_db",
        description="v3 memory DB — manage and query the hybrid-search SQLite memory store.",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help=f"DB path (default: {DB_PATH})",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="SUBCOMMAND")

    # init
    p_init = sub.add_parser("init", help="Create/migrate schema (idempotent).")

    # search
    p_search = sub.add_parser(
        "search",
        help="Search memories by query.",
    )
    p_search.add_argument("query", help="Free-text search query.")
    p_search.add_argument(
        "-k", type=int, default=5, metavar="N", help="Max results (default 5)."
    )
    p_search.add_argument(
        "--mode",
        choices=["hybrid", "fts", "vec"],
        default="hybrid",
        help="Search mode: hybrid (default), fts, or vec.",
    )
    p_search.add_argument(
        "--tier",
        default=None,
        help="Restrict results to this tier (use 'archive' to query archived rows).",
    )
    p_search.add_argument(
        "--all",
        action="store_true",
        dest="include_archived",
        help="Include archive-tier rows (excluded from default recall).",
    )
    p_search.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output machine-readable JSON array.",
    )

    # upsert
    p_upsert = sub.add_parser(
        "upsert",
        help="Insert or update a memory.",
    )
    p_upsert.add_argument("--tier", required=True, help="Tier: instance, shared, class, global.")
    p_upsert.add_argument(
        "--type",
        required=True,
        metavar="TYPE",
        dest="mem_type",
        help="Type: feedback, project, reference, user.",
    )
    p_upsert.add_argument("--name", required=True, help="Unique memory name/slug.")
    p_upsert.add_argument("--description", required=True, help="One-line description.")
    p_upsert.add_argument("--agent", default=None, help="Agent name (optional).")
    p_upsert.add_argument(
        "--path",
        default=None,
        help="Logical key path (default: <tier>/<agent-or-_>/<name>.md).",
    )
    txt_src = p_upsert.add_mutually_exclusive_group(required=True)
    txt_src.add_argument("--text-file", metavar="FILE", help="Read body text from FILE.")
    txt_src.add_argument(
        "--text-stdin",
        action="store_true",
        help="Read body text from stdin.",
    )

    # get
    p_get = sub.add_parser(
        "get",
        help="Print a memory's body (or rendered HTML).",
    )
    p_get_id = p_get.add_mutually_exclusive_group(required=True)
    p_get_id.add_argument("--id", type=int, metavar="N", help="Memory row id.")
    p_get_id.add_argument("--name", metavar="X", help="Memory name/slug.")
    p_get.add_argument(
        "--html",
        action="store_true",
        help="Output rendered HTML instead of raw text.",
    )

    # list
    p_list = sub.add_parser(
        "list",
        help="List memories (name · tier · type · path).",
    )
    p_list.add_argument("--tier", default=None, help="Filter by tier.")
    p_list.add_argument(
        "--type",
        default=None,
        metavar="TYPE",
        dest="mem_type",
        help="Filter by type.",
    )

    # prune
    p_prune = sub.add_parser(
        "prune",
        help="Delete a memory + its FTS/vec rows.",
    )
    p_prune_id = p_prune.add_mutually_exclusive_group(required=True)
    p_prune_id.add_argument("--id", type=int, metavar="N", help="Memory row id.")
    p_prune_id.add_argument("--name", metavar="X", help="Memory name/slug.")
    p_prune_id.add_argument(
        "--path", metavar="P", help="Memory logical path (UNIQUE identity)."
    )

    # stats
    p_stats = sub.add_parser("stats", help="Row counts for memories / fts / vec.")

    # similar
    p_similar = sub.add_parser(
        "similar",
        help="Find memories most similar (hybrid cosine + token-Jaccard) to one.",
    )
    p_similar_id = p_similar.add_mutually_exclusive_group(required=True)
    p_similar_id.add_argument("--id", type=int, metavar="N", help="Memory row id.")
    p_similar_id.add_argument("--name", metavar="X", help="Memory name/slug.")
    p_similar.add_argument(
        "-k", type=int, default=8, metavar="N", help="Max results (default 8)."
    )
    p_similar.add_argument(
        "--tier", default=None, help="Restrict candidates to this tier."
    )
    p_similar.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output machine-readable JSON array.",
    )

    # archive
    p_archive = sub.add_parser(
        "archive",
        help="Re-tier a memory to 'archive' in place (preserves row + embedding).",
    )
    p_archive_id = p_archive.add_mutually_exclusive_group(required=True)
    p_archive_id.add_argument("--id", type=int, metavar="N", help="Memory row id.")
    p_archive_id.add_argument("--name", metavar="X", help="Memory name/slug.")
    p_archive.add_argument(
        "--unarchive",
        action="store_true",
        help="Reverse: restore the original tier + a matching path.",
    )

    # retier
    p_retier = sub.add_parser(
        "retier",
        help="Deliberately re-tier a memory in place to a target tier "
        "(preserves row + embedding; no dup; e.g. promotion instance→shared-global).",
    )
    p_retier_id = p_retier.add_mutually_exclusive_group(required=True)
    p_retier_id.add_argument("--id", type=int, metavar="N", help="Memory row id.")
    p_retier_id.add_argument("--name", metavar="X", help="Memory name/slug.")
    p_retier.add_argument(
        "--tier",
        required=True,
        metavar="TARGET",
        help="Target tier (e.g. shared-global). Reject 'archive' — use `archive`.",
    )

    # export
    p_export = sub.add_parser(
        "export",
        help="Emit a JSON manifest of all rows (for cross-device sync).",
    )
    p_export.add_argument(
        "--with-body",
        action="store_true",
        help="Include each row's full body text (default: metadata only).",
    )

    ns = parser.parse_args()

    db = Path(ns.db) if ns.db else DB_PATH

    if ns.cmd is None:
        parser.print_help()
        sys.exit(0)

    if ns.cmd == "init":
        init_db(db)
        print(f"init_db ok → {db}")

    elif ns.cmd == "search":
        results = search(
            ns.query,
            k=ns.k,
            mode=ns.mode,
            db_path=db,
            include_archived=ns.include_archived,
            tier=ns.tier,
        )
        if ns.as_json:
            # Omit html column from machine output (large, rarely needed).
            def _clean(d: dict) -> dict:
                d2 = dict(d)
                d2.pop("html", None)
                return d2
            print(json.dumps([_clean(r) for r in results], ensure_ascii=False, indent=2))
        else:
            if not results:
                print("(no results)")
            for r in results:
                snippet = (r.get("text") or "").replace("\n", " ")[:120]
                print(
                    f"[{r['id']:>4}] {r['name']:<40}  tier={r.get('tier') or '-':<18}"
                    f"  type={r.get('type') or '-':<10}  score={r['score']:.4f}"
                )
                print(f"       path: {r.get('path') or '-'}")
                print(f"       {snippet}")
                print()

    elif ns.cmd == "upsert":
        if ns.text_stdin:
            body = sys.stdin.read()
        else:
            p = Path(ns.text_file)
            if not p.exists():
                _die(f"--text-file not found: {p}")
            body = p.read_text(encoding="utf-8", errors="replace")

        agent_part = ns.agent or "_"
        logical_path = ns.path or f"{ns.tier}/{agent_part}/{ns.name}.md"

        row_id = upsert(
            path=logical_path,
            tier=ns.tier,
            agent=ns.agent,
            type=ns.mem_type,
            name=ns.name,
            description=ns.description,
            text=body,
            db_path=db,
        )
        print(f"upserted id={row_id}  path={logical_path}")

    elif ns.cmd == "get":
        conn = _connect(db)
        try:
            if ns.id is not None:
                row = _db_get_by_id(conn, ns.id)
                row_id = ns.id
            else:
                row = _db_get_by_name(conn, ns.name)
                row_id = row["id"] if row else None
        finally:
            conn.close()

        if row is None:
            ref = f"id={ns.id}" if ns.id is not None else f"name={ns.name!r}"
            _die(f"memory not found: {ref}")

        if ns.html:
            print(get_html(row_id, db_path=db))
        else:
            print(row["text"])

    elif ns.cmd == "list":
        conn = _connect(db)
        try:
            query_parts = ["SELECT id, name, tier, type, path FROM memories WHERE 1=1"]
            params: list = []
            if ns.tier:
                query_parts.append("AND tier = ?")
                params.append(ns.tier)
            if ns.mem_type:
                query_parts.append("AND type = ?")
                params.append(ns.mem_type)
            query_parts.append("ORDER BY tier, name")
            rows = conn.execute(" ".join(query_parts), params).fetchall()
        finally:
            conn.close()

        if not rows:
            print("(no memories match)")
        else:
            for r in rows:
                print(
                    f"{r['name']:<45}  tier={r['tier'] or '-':<18}"
                    f"  type={r['type'] or '-':<12}  {r['path'] or '-'}"
                )

    elif ns.cmd == "prune":
        conn = _connect(db)
        try:
            if ns.id is not None:
                row = _db_get_by_id(conn, ns.id)
            elif ns.path is not None:
                row = _db_get_by_path(conn, ns.path)
            else:
                row = _db_get_by_name(conn, ns.name)
        finally:
            conn.close()

        if row is None:
            if ns.id is not None:
                ref = f"id={ns.id}"
            elif ns.path is not None:
                ref = f"path={ns.path!r}"
            else:
                ref = f"name={ns.name!r}"
            _die(f"memory not found: {ref}")

        row_id = row["id"]
        name_label = row["name"]
        _prune_row(row_id, db)
        print(f"pruned id={row_id}  name={name_label!r}")

    elif ns.cmd == "similar":
        conn = _connect(db)
        try:
            if ns.id is not None:
                row = _db_get_by_id(conn, ns.id)
            else:
                row = _db_get_by_name(conn, ns.name)
        finally:
            conn.close()
        if row is None:
            ref = f"id={ns.id}" if ns.id is not None else f"name={ns.name!r}"
            _die(f"memory not found: {ref}")

        try:
            results = similar(row["id"], k=ns.k, tier=ns.tier, db_path=db)
        except LookupError as e:
            _die(str(e))

        if ns.as_json:
            def _clean(d: dict) -> dict:
                d2 = dict(d)
                d2.pop("html", None)
                return d2
            print(json.dumps([_clean(r) for r in results], ensure_ascii=False, indent=2))
        else:
            print(f"similar to [{row['id']}] {row['name']!r}:")
            if not results:
                print("(no candidates)")
            for r in results:
                desc = (r.get("description") or "").replace("\n", " ")[:70]
                print(
                    f"  combined={r['combined']:.4f}  cos={r['cosine']:.4f}"
                    f"  jac={r['jaccard']:.4f}  {r['name']:<40}"
                    f"  tier={r.get('tier') or '-':<16}"
                )
                print(f"      {desc}")

    elif ns.cmd == "archive":
        conn = _connect(db)
        try:
            if ns.id is not None:
                row = _db_get_by_id(conn, ns.id)
            else:
                row = _db_get_by_name(conn, ns.name)
        finally:
            conn.close()
        if row is None:
            ref = f"id={ns.id}" if ns.id is not None else f"name={ns.name!r}"
            _die(f"memory not found: {ref}")

        try:
            info = archive(row["id"], unarchive=ns.unarchive, db_path=db)
        except (LookupError, ValueError) as e:
            _die(str(e))

        verb = "unarchived" if ns.unarchive else "archived"
        print(
            f"{verb} id={info['id']}  name={info['name']!r}\n"
            f"  tier: {info['old_tier']} → {info['new_tier']}\n"
            f"  path: {info['old_path']} → {info['new_path']}"
        )

    elif ns.cmd == "retier":
        conn = _connect(db)
        try:
            if ns.id is not None:
                row = _db_get_by_id(conn, ns.id)
            else:
                row = _db_get_by_name(conn, ns.name)
        finally:
            conn.close()
        if row is None:
            ref = f"id={ns.id}" if ns.id is not None else f"name={ns.name!r}"
            _die(f"memory not found: {ref}")

        try:
            info = retier(row["id"], ns.tier, db_path=db)
        except (LookupError, ValueError) as e:
            _die(str(e))

        print(
            f"retiered id={info['id']}  name={info['name']!r}\n"
            f"  tier: {info['old_tier']} → {info['new_tier']}\n"
            f"  path: {info['old_path']} → {info['new_path']}"
        )

    elif ns.cmd == "stats":
        conn = _connect(db)
        try:
            mem_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            fts_count = conn.execute(
                "SELECT COUNT(*) FROM memories_fts"
            ).fetchone()[0]
            vec_count = conn.execute(
                "SELECT COUNT(*) FROM memories_vec"
            ).fetchone()[0]
        finally:
            conn.close()

        print(f"memories : {mem_count}")
        print(f"fts rows : {fts_count}")
        print(f"vec rows : {vec_count}")

    elif ns.cmd == "export":
        # JSON manifest of all rows for cross-device sync. Identity = `path`,
        # equality = content `hash` (sha256 of name+description+text — machine
        # independent), recency = `updated`. `id`/`created` are intentionally
        # omitted from the equality surface (machine-local); `--with-body`
        # adds `text` so a remote peer can reconstruct a row via `upsert`.
        conn = _connect(db)
        try:
            cols = "path, name, tier, type, agent, description, hash, updated"
            if ns.with_body:
                cols += ", text"
            rows = conn.execute(
                f"SELECT {cols} FROM memories ORDER BY path"
            ).fetchall()
        finally:
            conn.close()
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
