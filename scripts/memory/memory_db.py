"""v3 Memory subsystem — hybrid-search SQLite DB.

Hybrid keyword (FTS5 BM25) + semantic (sqlite-vec KNN) search over the curated
agent-memory corpus. This DB is the sole store and source of truth for agent
memory; `migrate` only ingests legacy flat-MD trees. Deleting the .db file loses
every memory, so back up with `mem export --with-body` (a JSON dump that
`upsert` can restore from).

Invoke via the `mem` shim (or `~/.claude/.venv/Scripts/python.exe` on Windows);
fastembed + sqlite-vec live in that venv.

Public API: init_db, upsert, upsert_many, search, get_html, migrate.
"""

from __future__ import annotations

import datetime
import hashlib
import html as _html
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

# sc_profile vive in <code home>/lib: il percorso si ricava da questo file, cosi'
# funziona sia dall'albero live sia da una copia di staging
_LIB_DIR = str(Path(__file__).resolve().parents[2] / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import sc_profile  # noqa: E402
# db_wall sta accanto a questo file: serve anche quando il modulo e' caricato da percorso
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from db_wall import CrossProfileDBError, assert_own_profile_path  # noqa: E402,F401

try:
    import sqlite_vec
except ImportError:  # il sottocomando `sql` e i tool di sola lettura non ne hanno bisogno
    sqlite_vec = None

# --- Constants ---------------------------------------------------------------

# DB del profilo attivo (SC_PROFILE); resta una costante di modulo perche' i test la sostituiscono
DB_PATH = sc_profile.memory_db_path()
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
VEC_CHUNK_SIZE = 256  # vec0 chunk slots; tight fit for our corpus (~393KB/256 rows @384-dim vs 1.57MB at the 1024 default). Single source of truth — used in _SCHEMA AND compact().
# Pinned, persistent model cache. The FastEmbed default ($TMPDIR/fastembed_cache)
# is ephemeral AND differs per shell/sandbox, causing "model not found" across
# contexts (warm-up shell vs hook/skill runtime). Pin it so it is found everywhere.
# Codice condiviso, non dato: tutti i profili usano lo stesso modello (anche offline).
EMBED_CACHE_DIR = sc_profile.code_home() / ".cache" / "fastembed"
# --- Hybrid retrieval tuning (ALL query-time; stored vectors are NOT affected) ---
# Reciprocal Rank Fusion constant. Lower k => sharper emphasis on each arm's top
# ranks. Tuned to the 20-30 band (was 60): the corpus is small and jargon-dense, so
# a sharper fusion pulls the right note up without the smoothing a large k imposes.
RRF_K = 30
# EQUAL fusion weights. Notes are jargon / path / error-code heavy, so the lexical
# (FTS/BM25) arm is as trustworthy as the semantic (vector) arm; never down-weight it.
RRF_W_FTS = 1.0
RRF_W_VEC = 1.0
# Minimum candidates pulled from EACH arm before fusion. A miss is usually the right
# note dropping out of one arm's narrow head; a wide pool lets the other arm recover
# it during fusion. Acts as a floor under the existing k*4 over-fetch.
CANDIDATE_POOL = 50
# bge-small-en-v1.5 is ASYMMETRIC: the query carries a short retrieval instruction,
# passages are embedded bare. Stored passage vectors here are bare (see
# _refresh_embedding), so ONLY the query embedding gets this prefix at search
# time; no stored vector is mutated. Text is bge-small-en-v1.5's documented prompt.
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
ARCHIVE_TIER = "archive"  # Re-tiered, out-of-active-recall memories (still queryable).
# Tier canonici e forme brevi accettate in ingresso (vedi normalize_tier).
CANONICAL_TIERS = ("instance", "class", "shared-projects", "shared-global", ARCHIVE_TIER)
TIER_ALIASES = {"shared": "shared-projects", "global": "shared-global"}
# Realm: l'unico valore memorizzato e' 'personal'; NULL = riga senza tag. SC_MEM_REALM=personal
# fa nascere personal le righe nuove (vale per ogni scrittore, plan_index compreso).
REALM_PERSONAL = "personal"
REALM_ENV_VAR = "SC_MEM_REALM"
REALM_FILTERS = ("all", REALM_PERSONAL, "untagged")  # valori di --realm per search e list
EMBED_BATCH_SIZE = 32  # testi per chiamata al modello in upsert_many
BUSY_TIMEOUT_MS = 5000  # attesa massima di un writer concorrente prima di rinunciare

# Migration walk rules (spec §Migration rules).
_EXCLUDE_PARTS = {"_system", "_archive", "archive", "_compact-snapshots", "_stop-snapshots"}


def normalize_tier(tier: str) -> str:
    """Riporta un nome di tier alla forma canonica.

    Accetta le forme brevi `shared` e `global`, ignora maiuscole e spazi ai
    bordi; i nomi canonici passano invariati. Un nome sconosciuto solleva
    ValueError con l'elenco dei tier validi (la CLI lo traduce in exit code 2).
    """
    key = (tier or "").strip().lower()
    key = TIER_ALIASES.get(key, key)
    if key not in CANONICAL_TIERS:
        raise ValueError(
            f"unknown tier {tier!r}; valid tiers: {', '.join(CANONICAL_TIERS)} "
            f"(aliases: shared -> shared-projects, global -> shared-global)"
        )
    return key


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


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed several documents with ONE model call; one 384-dim float list per text."""
    return [[float(x) for x in vec] for vec in _get_embedder().embed(list(texts))]


def _embed(text: str) -> list[float]:
    """Embed one document → 384-dim float list."""
    return _embed_batch([text])[0]


def _embed_query(text: str) -> list[float]:
    """Embed a QUERY with bge-small's retrieval instruction prefix (asymmetric).

    Passages are stored bare; only the query side carries the instruction, matching
    how bge-small-en-v1.5 was trained for retrieval. Stored vectors are untouched;
    this only shapes the query vector at search time.
    """
    return _embed(BGE_QUERY_INSTRUCTION + (text or ""))


# --- Connection --------------------------------------------------------------


def _connect(db_path: Path | str = DB_PATH, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection with the sqlite-vec extension loaded.

    A writable connection also brings the schema up to date (see `_migrate_schema`).
    `readonly=True` opens the file read-only and never migrates.
    """
    # Muro tra profili prima di creare cartelle o file
    path = Path(db_path).expanduser()
    assert_own_profile_path(path)
    if readonly:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        # Su un'installazione nuova ~/.claude/agent-memory/ non esiste ancora:
        # sqlite3 non crea le cartelle e fallirebbe con "unable to open database file".
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
    # Wait for a competing writer instead of failing immediately with
    # "database is locked" (Phase EM concurrency hardening).
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    if not readonly:
        _migrate_schema(conn)  # dopo vec: ALTER rilegge tutto lo schema, tabelle vec0 incluse
    return conn


# --- Migrazione schema -------------------------------------------------------

# Codici SQLite primari per cui la migrazione si rimanda alla connessione successiva
_SQLITE_BUSY, _SQLITE_LOCKED, _SQLITE_READONLY = 5, 6, 8


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of `table` (empty set if the table does not exist)."""
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _has_realm(conn: sqlite3.Connection) -> bool:
    return "realm" in _table_columns(conn, "memories")


def _is_busy_or_readonly(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in (_SQLITE_BUSY, _SQLITE_LOCKED, _SQLITE_READONLY)
    message = str(exc).lower()
    return any(word in message for word in ("locked", "busy", "readonly"))


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add `memories.realm` to a DB created before the column existed. Idempotent.

    Never fatal for the three benign cases: a concurrent process added the column
    between our check and the ALTER (duplicate column), a writer holds the lock, or the
    file is read-only. In the last two the column stays absent and the next writable
    connect retries; every realm read and filter checks the column first.
    Any other error propagates.
    """
    try:
        columns = _table_columns(conn, "memories")
        if columns and "realm" not in columns:
            conn.execute("ALTER TABLE memories ADD COLUMN realm TEXT")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower() or _is_busy_or_readonly(exc):
            return
        raise


# --- Schema ------------------------------------------------------------------

_SCHEMA = f"""
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
  hash TEXT,
  realm TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  name, description, text,
  content='memories', content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(embedding float[{EMBED_DIM}], chunk_size={VEC_CHUNK_SIZE});

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


# Sentinella di `realm` non passato: la riga esistente conserva il suo, una nuova prende
# quello dell'ambiente. Un None esplicito significa invece "togli il tag".
_UNSET = object()
# Campi il cui cambiamento fa avanzare `updated` (a loro si somma realm, se la colonna c'e')
_BUMP_FIELDS = ("tier", "agent", "type", "name", "description", "text")
_ROW_REQUIRED_KEYS = frozenset({"path", "tier", "type", "name", "description", "text"})
_ROW_KEYS = _ROW_REQUIRED_KEYS | {"agent", "created", "updated", "realm"}
# Forma unica ammessa per i timestamp: solo offset +00:00, cosi' le stringhe si
# ordinano come gli istanti (con offset diversi l'ordine lessicale non lo garantisce)
_ISO_OFFSET_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?\+00:00\Z", re.ASCII
)


def _validate_timestamp(value, label: str) -> str | None:
    """Return `value` unchanged if it is ISO-8601 with the +00:00 offset, else raise ValueError.

    None passes through (caller means "use now"). Naive times, `Z`, other offsets and
    other separators are rejected so stored strings stay uniform and sortable.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not _ISO_OFFSET_RE.match(value):
        raise ValueError(
            f"{label} must be ISO-8601 with the +00:00 offset, e.g. 2026-10-15T17:30:00+00:00; "
            f"got {value!r}"
        )
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid date/time: {value!r}") from exc
    return value


def _normalize_realm(value) -> str | None:
    """Map an explicit realm to its stored form: 'personal' or None ('untagged'/None)."""
    if value is None:
        return None
    key = value.strip().lower() if isinstance(value, str) else None
    if key == REALM_PERSONAL:
        return REALM_PERSONAL
    if key == "untagged":
        return None
    raise ValueError(f"unknown realm {value!r}; valid values: {REALM_PERSONAL}, untagged")


def _default_realm() -> str | None:
    """Realm of a NEW row when the caller gives none, from the environment."""
    env = os.environ.get(REALM_ENV_VAR, "").strip().lower()
    return REALM_PERSONAL if env == REALM_PERSONAL else None


def _resolve_realm(requested, current, is_new: bool, has_realm: bool) -> str | None:
    """Realm to store: explicit value, else the env default (new row) or the current one.

    Refuses to write a personal row where the column is missing: dropping the tag
    silently would leak the row into work sessions.
    """
    if requested is not _UNSET:
        realm = requested
    elif is_new:
        realm = _default_realm()
    else:
        realm = current
    if realm == REALM_PERSONAL and not has_realm:
        raise RuntimeError(
            "memories.realm column is missing (schema migration was skipped): "
            "refusing to write a personal row without its tag"
        )
    return realm


def _normalize_row(
    *,
    path,
    tier,
    type,
    name,
    description,
    text,
    agent=None,
    created=None,
    updated=None,
    realm=_UNSET,
) -> dict:
    """Validate and normalise one row; raises ValueError before anything is written."""
    return {
        "path": str(path),
        "tier": normalize_tier(tier),
        "agent": agent,
        "type": type,
        "name": name,
        "description": description,
        "text": text,
        "hash": _content_hash(text, name, description),
        "created": _validate_timestamp(created, "created"),
        "updated": _validate_timestamp(updated, "updated"),
        "realm": realm if realm is _UNSET else _normalize_realm(realm),
    }


def _normalize_batch(rows) -> list[dict]:
    """Normalise every row of a batch up front: a bad row fails before any write."""
    batch: list[dict] = []
    for index, raw in enumerate(rows):
        unknown, missing = set(raw) - _ROW_KEYS, _ROW_REQUIRED_KEYS - set(raw)
        if unknown or missing:
            raise ValueError(
                f"row {index}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}"
            )
        try:
            batch.append(_normalize_row(**raw))
        except ValueError as exc:
            raise ValueError(f"row {index}: {exc}") from exc
    return batch


def upsert(
    path: str,
    tier: str,
    agent: str | None,
    type: str,
    name: str,
    description: str,
    text: str,
    db_path: Path | str = DB_PATH,
    created: str | None = None,
    updated: str | None = None,
    realm=_UNSET,
) -> int:
    """Insert or update a memory keyed by `path`; commits. Returns the row id.

    `created` / `updated`: ISO-8601 with the +00:00 offset, stored verbatim (ValueError
    otherwise); omitted means now. On an update `updated` advances only when tier, agent,
    type, name, description, text or realm change, and an upsert that changes nothing
    writes nothing. A new or content-changed row is (re-)embedded into memories_vec and
    its links refreshed; html is left NULL (rendered lazily by get_html).

    `realm`: 'personal' tags the row, 'untagged' or None clears it. Omitted: a new row is
    personal iff SC_MEM_REALM=personal, an existing row keeps its realm.
    """
    row = _normalize_row(
        path=path, tier=tier, agent=agent, type=type, name=name,
        description=description, text=text, created=created, updated=updated, realm=realm,
    )
    conn = _connect(db_path)
    try:
        row_id = _write_row(conn, row, _has_realm(conn))
        conn.commit()
        return row_id
    finally:
        conn.close()


def upsert_many(rows, conn: sqlite3.Connection, embed: bool = True) -> list[int]:
    """Upsert a batch of rows on `conn` WITHOUT committing; returns the row ids in order.

    Each row is a dict with the keyword names of `upsert` (required: path, tier, type,
    name, description, text; optional: agent, created, updated, realm). The caller owns
    the transaction (`BEGIN IMMEDIATE ... COMMIT`, or rollback to undo the whole batch).
    All rows are validated first, then every needed embedding is computed in batches of
    EMBED_BATCH_SIZE, and only then are rows written. Validation, bump rule and realm
    rules are those of `upsert`.

    `embed=False` writes no vectors and never touches memories_vec (meant for fresh DBs
    such as a restore drill; a text change on an existing row would keep its old vector).
    """
    batch = _normalize_batch(rows)
    # Vale anche per autocommit=True (PEP 249), che lascia isolation_level == ''
    if not conn.in_transaction:
        raise RuntimeError(
            "upsert_many needs an open transaction: run BEGIN IMMEDIATE first, "
            "otherwise rows can commit one by one"
        )
    has_realm = _has_realm(conn)
    vectors = _precompute_vectors(conn, batch) if embed else {}
    return [
        _write_row(conn, row, has_realm, vectors.get(index), embed)
        for index, row in enumerate(batch)
    ]


def _precompute_vectors(conn: sqlite3.Connection, batch: list[dict]) -> dict[int, list[float]]:
    """Embed, in batches, the text of every row that is new or whose content hash changes."""
    pending = []
    for index, row in enumerate(batch):
        old = conn.execute("SELECT hash FROM memories WHERE path = ?", (row["path"],)).fetchone()
        if old is None or old["hash"] != row["hash"]:
            pending.append(index)
    vectors: dict[int, list[float]] = {}
    for start in range(0, len(pending), EMBED_BATCH_SIZE):
        chunk = pending[start:start + EMBED_BATCH_SIZE]
        embedded = _embed_batch([batch[index]["text"] for index in chunk])
        vectors.update(zip(chunk, embedded))
    return vectors


def _write_row(
    conn: sqlite3.Connection,
    row: dict,
    has_realm: bool,
    vector: list[float] | None = None,
    embed: bool = True,
) -> int:
    """Insert or update one normalised row WITHOUT committing; returns its id.

    `vector` is the precomputed embedding (None: computed here when needed).
    """
    select = ", ".join(("id", "hash", "created", "updated", *_BUMP_FIELDS))
    select += ", realm" if has_realm else ", NULL AS realm"
    existing = conn.execute(
        f"SELECT {select} FROM memories WHERE path = ?", (row["path"],)
    ).fetchone()
    if existing is None:
        row_id, content_changed = _insert_row(conn, row, has_realm), True
    else:
        row_id, content_changed = _update_row(conn, existing, row, has_realm)
    if content_changed:
        if embed:
            _refresh_embedding(conn, row_id, row["text"], vector)
        _refresh_links(conn, row_id, row["text"])
    return row_id


def _insert_row(conn: sqlite3.Connection, row: dict, has_realm: bool) -> int:
    now = _now()
    values = {column: row[column] for column in ("path", *_BUMP_FIELDS, "hash")}
    values.update(html=None, created=row["created"] or now, updated=row["updated"] or now)
    realm = _resolve_realm(row["realm"], None, True, has_realm)
    if has_realm:
        values["realm"] = realm
    cur = conn.execute(
        f"INSERT INTO memories ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        list(values.values()),
    )
    return cur.lastrowid


def _update_row(
    conn: sqlite3.Connection, existing: sqlite3.Row, row: dict, has_realm: bool
) -> tuple[int, bool]:
    """Write only what differs; returns (row id, whether name/description/text changed)."""
    candidate = {field: row[field] for field in _BUMP_FIELDS}
    realm = _resolve_realm(row["realm"], existing["realm"], False, has_realm)
    if has_realm:
        candidate["realm"] = realm
    bump = any(existing[column] != value for column, value in candidate.items())
    candidate["hash"] = row["hash"]
    if row["created"] is not None:
        candidate["created"] = row["created"]
    if row["updated"] is not None:
        candidate["updated"] = row["updated"]
    elif bump:
        candidate["updated"] = _now()
    changed = {column: value for column, value in candidate.items() if existing[column] != value}
    content_changed = "hash" in changed
    if content_changed:
        changed["html"] = None  # il testo e' cambiato: l'html in cache non vale piu'
    if changed:
        assignments = ", ".join(f"{column} = ?" for column in changed)
        conn.execute(
            f"UPDATE memories SET {assignments} WHERE id = ?",
            [*changed.values(), existing["id"]],
        )
    return existing["id"], content_changed


def _refresh_embedding(
    conn: sqlite3.Connection, row_id: int, text: str, vector: list[float] | None = None
) -> None:
    """(Re)embed `text`, or store the precomputed `vector`, in memories_vec (rowid == memories.id)."""
    embedding = _embed(text) if vector is None else vector
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
    realm: str = "all",
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

    `realm` ('all' | 'personal' | 'untagged', CLI: `--realm`) keeps only the rows of that
    realm; a DB without the realm column has no personal rows.
    """
    _check_realm_filter(realm)
    if tier is not None:
        tier = normalize_tier(tier)
    # tier='archive' implies the caller wants archived rows; never exclude then.
    exclude_archive = not include_archived and tier != ARCHIVE_TIER
    # Over-fetch when filtering so the post-filter still yields up to `k`.
    fetch_k = max(k * 4, k) if (exclude_archive or tier is not None or realm != "all") else k
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
        scored = _apply_tier_filter(conn, scored, exclude_archive, tier, realm)[:k]
        return _materialize(conn, scored)
    finally:
        conn.close()


def search_many(
    queries,
    k: int = 8,
    mode: str = "hybrid",
    db_path: Path | str = DB_PATH,
    include_archived: bool = False,
    tier: str | None = None,
    realm: str = "all",
) -> list[dict]:
    """Union hybrid-search over MULTIPLE query strings, de-duped by note identity.

    Each query is embedded + retrieved independently via `search`; the per-query
    result lists are unioned and de-duplicated by row id, KEEPING each note's BEST
    (max) `score` across the queries, then re-ranked best-first and capped at `k`.

    `queries` may be a single str (treated as one query) or a sequence of strings;
    blank/whitespace-only entries are ignored. A single effective query is delegated
    straight to `search`, so single-query behavior is byte-for-byte unchanged.
    """
    if isinstance(queries, str):
        queries = [queries]
    qs = [q for q in queries if q and q.strip()]
    if not qs:
        return []
    if len(qs) == 1:
        return search(
            qs[0], k=k, mode=mode, db_path=db_path,
            include_archived=include_archived, tier=tier, realm=realm,
        )
    best: dict[int, dict] = {}
    for q in qs:
        for r in search(
            q, k=k, mode=mode, db_path=db_path,
            include_archived=include_archived, tier=tier, realm=realm,
        ):
            rid = r["id"]
            if rid not in best or r["score"] > best[rid]["score"]:
                best[rid] = r
    return sorted(best.values(), key=lambda d: d["score"], reverse=True)[:k]


def _check_realm_filter(realm: str) -> None:
    if realm not in REALM_FILTERS:
        raise ValueError(
            f"unknown realm filter {realm!r}; valid values: {', '.join(REALM_FILTERS)}"
        )


def _realm_predicate(conn: sqlite3.Connection, realm: str) -> str:
    """SQL condition on memories.realm for a filter in REALM_FILTERS (constants only).

    Gated on the column: a DB that was not migrated has no personal row, so
    'personal' matches nothing there and 'untagged' matches everything.
    """
    if realm == "all":
        return "1"
    if not _has_realm(conn):
        return "0" if realm == REALM_PERSONAL else "1"
    if realm == REALM_PERSONAL:
        return f"realm = '{REALM_PERSONAL}'"
    return f"COALESCE(realm, '') != '{REALM_PERSONAL}'"


def _apply_tier_filter(
    conn: sqlite3.Connection,
    scored: list[tuple[int, float]],
    exclude_archive: bool,
    tier: str | None,
    realm: str = "all",
) -> list[tuple[int, float]]:
    """Drop ranked (rowid, score) pairs failing the archive-exclusion/tier/realm filter.

    Preserves rank order. A single lookup of the candidate rowids' tiers avoids
    re-querying per row. No-op when no constraint is active.
    """
    if not exclude_archive and tier is None and realm == "all":
        return scored
    ids = [rid for rid, _ in scored]
    if not ids:
        return scored
    placeholders = ",".join("?" * len(ids))
    tier_by_id = {
        r["id"]: r["tier"]
        for r in conn.execute(
            f"SELECT id, tier FROM memories WHERE id IN ({placeholders}) "
            f"AND {_realm_predicate(conn, realm)}",
            ids,
        ).fetchall()
    }
    out: list[tuple[int, float]] = []
    for rid, score in scored:
        if rid not in tier_by_id:
            continue
        row_tier = tier_by_id[rid]
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
    q = sqlite_vec.serialize_float32(_embed_query(query))
    rows = conn.execute(
        """SELECT rowid, distance
           FROM memories_vec
           WHERE embedding MATCH ? AND k = ?
           ORDER BY distance""",
        (q, k),
    ).fetchall()
    return [(r["rowid"], r["distance"]) for r in rows]


def _hybrid_scored(
    conn: sqlite3.Connection,
    query: str,
    k: int,
    *,
    rrf_k: float | None = None,
    w_fts: float | None = None,
    w_vec: float | None = None,
    pool: int | None = None,
) -> list[tuple[int, float]]:
    """Fuse FTS and vec ranked lists with weighted Reciprocal Rank Fusion.

        score(doc) = w_fts / (rrf_k + rank_fts) + w_vec / (rrf_k + rank_vec)

    Ranks are 0-based per arm (best = 0); a doc absent from an arm contributes 0
    from that arm. Each arm over-fetches a WIDE candidate pool of max(k*4, pool)
    BEFORE fusion, so a note that falls out of one arm's narrow head can still be
    recovered by the other arm. Returns the fused (rowid, score) list best-first,
    capped at k, unmaterialized so callers can post-filter on rowid metadata.

    rrf_k / w_fts / w_vec / pool default to the module constants; they are exposed
    as parameters so the retrieval path stays tunable and unit-testable.
    """
    rrf_k = RRF_K if rrf_k is None else rrf_k
    w_fts = RRF_W_FTS if w_fts is None else w_fts
    w_vec = RRF_W_VEC if w_vec is None else w_vec
    pool = CANDIDATE_POOL if pool is None else pool
    fetch = max(k * 4, k, pool)
    fts = _fts_ranked(conn, query, fetch)
    vec = _vec_ranked(conn, query, fetch)

    scores: dict[int, float] = {}
    for rank, (rid, _) in enumerate(fts):
        scores[rid] = scores.get(rid, 0.0) + w_fts / (rrf_k + rank)
    for rank, (rid, _) in enumerate(vec):
        scores[rid] = scores.get(rid, 0.0) + w_vec / (rrf_k + rank)

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
    # Anche il corpus MD sorgente e' memoria: mai leggere quello di un altro profilo
    root = assert_own_profile_path(Path(root))
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
    # (names can collide across tiers).
    return conn.execute(
        "SELECT * FROM memories WHERE path = ?", (path,)
    ).fetchone()


def _any_agent_reuse(tier: str, agent: str | None) -> bool:
    """Regola unica: senza --agent, solo nel tier shared-global, il riuso ignora l'agent."""
    return agent is None and tier == "shared-global"


def _reusable_row(
    conn: sqlite3.Connection, tier: str, agent: str | None, name: str, default_path: str
):
    """Riga esistente che un `upsert` della CLI senza --path deve aggiornare, oppure None.

    Solo se al percorso di default non c'e' nessuna riga ed ESATTAMENTE UNA riga viva
    (non archiviata) ha lo stesso name, tier e agent. Unica eccezione: senza --agent
    (`agent` None) nel tier shared-global va bene qualunque agent, perche' le skill salvano
    shared-global senza --agent righe importate con agent valorizzato; negli altri tier
    agent None vale solo per le righe con agent NULL. E' il caso delle righe importate con
    un nome di file diverso dal name, che un nuovo salvataggio per nome duplicherebbe.
    """
    if _db_get_by_path(conn, default_path) is not None:
        return None
    realm_col = "realm" if _has_realm(conn) else "NULL AS realm"
    sql = f"SELECT id, tier, path, agent, {realm_col} FROM memories WHERE name = ? AND tier = ?"
    params: tuple = (name, tier)
    if not _any_agent_reuse(tier, agent):
        sql += " AND agent IS ?"
        params += (agent,)
    rows = conn.execute(sql, params).fetchall()
    live = [r for r in rows if not _is_archived_row(r)]
    return live[0] if len(live) == 1 else None


# --- Name resolution ladder --------------------------------------------------
# A --name argument may be the stored human title ("European academic writing
# style"), the filename slug (Path(path).stem, e.g. "feedback_european_style"),
# a case variant, or a unique prefix of either. Resolve through a strict ladder
# that AUTO-RESOLVES ONLY ON A UNIQUE MATCH and never guesses; an ambiguous or
# unknown name yields "did you mean" candidates for the caller to surface.


def _slug_of(path: str) -> str:
    """Path-stem slug of a memory's logical path (filename without the .md)."""
    return Path(path or "").stem


def _fts_name_desc(conn: sqlite3.Connection, arg: str, limit: int) -> list[int]:
    """Up to `limit` rowids whose name/description best match `arg` (BM25).

    Restricts the MATCH to the name + description columns (the 'title' surface) and
    splits underscore slugs into words, so 'feedback_european_style' still matches a
    note titled 'European ... style'. Returns [] on any FTS syntax edge case.
    """
    words: list[str] = []
    for tok in _FTS_TOKEN_RE.findall(arg or ""):
        words.extend(w for w in tok.split("_") if w)
    if not words:
        return []
    match = "{name description} : (" + " OR ".join(f'"{w}"' for w in words) + ")"
    try:
        rows = conn.execute(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? "
            "ORDER BY bm25(memories_fts) LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r["rowid"] for r in rows]


def _did_you_mean(
    idx, arg: str, fts_ids: list[int], limit: int, tied=()
) -> list[dict]:
    """Build (name, slug, tier) suggestions for an unresolved name.

    Le righe a pareggio (`tied`) vengono sempre per prime e tutte, anche oltre `limit`:
    servono per rilanciare il comando con --id. Poi i candidati FTS (gia' ordinati
    BM25) e le corrispondenze per sottostringa su name o slug, fino a
    max(`limit`, numero di righe a pareggio) suggerimenti in totale.
    """
    by_id = {r["id"]: r for r in idx}
    cap = max(limit, len(tied))
    ordered: list = sorted(tied, key=lambda r: r["id"])
    seen: set[int] = {r["id"] for r in ordered}
    for rid in fts_ids:
        r = by_id.get(rid)
        if r is not None and rid not in seen:
            ordered.append(r)
            seen.add(rid)
    low = (arg or "").lower()
    if low:
        for r in idx:
            if r["id"] in seen:
                continue
            if low in (r["name"] or "").lower() or low in _slug_of(r["path"]).lower():
                ordered.append(r)
                seen.add(r["id"])
            if len(ordered) >= cap:
                break
    return [
        {"id": r["id"], "name": r["name"], "slug": _slug_of(r["path"]), "tier": r["tier"]}
        for r in ordered[:cap]
    ]


_DB_MAY_BE_MISSING_CMDS = frozenset({"init"})


def _is_archived_row(row) -> bool:
    return row["tier"] == ARCHIVE_TIER or (row["path"] or "").startswith("archive/")


def _resolve_name(
    conn: sqlite3.Connection, arg: str, limit: int = 5, prefer_live: bool = False
) -> tuple[int | None, list[dict]]:
    """Resolve a --name argument to ONE memory rowid via a strict ladder.

    Ladder (the first step with a match decides; never guesses):
      1. exact `name`
      2. path-stem slug            (Path(path).stem == arg)  [human-title-vs-file fix]
      3. steps 1-2 case-insensitively
      4. prefix on name OR slug
      5. FTS over name/description  (resolve only if exactly one candidate)

    Un passo con UNA riga risolve; con 0 righe si passa al successivo; con 2 o piu'
    il lookup TERMINA li' (nessuna ricaduta sui passi seguenti, che potrebbero
    puntare a una sola delle righe a pareggio).

    Returns (rowid, []) on a unique resolve, else (None, suggestions) where
    suggestions is the "did you mean" list of (id, name, slug, tier) dicts: on a tie
    every tied row comes first, then other suggestions up to `limit`.

    `prefer_live=True` (solo lettura: get/similar): su un pareggio resolve solo se
    ESATTAMENTE UNA delle righe di quel passo e' non archiviata. I comandi
    distruttivi lasciano False e restano su exit 3 a ogni pareggio.
    """
    arg = arg or ""
    if not arg.strip():
        return None, []
    low = arg.lower()
    idx = conn.execute(
        "SELECT id, name, description, tier, path FROM memories"
    ).fetchall()

    def step_matches():
        yield [r for r in idx if r["name"] == arg]
        # path-stem slug (the canonical fix: filename slug vs stored human title)
        yield [r for r in idx if _slug_of(r["path"]) == arg]
        yield [r for r in idx if (r["name"] or "").lower() == low]
        yield [r for r in idx if _slug_of(r["path"]).lower() == low]
        if low:
            yield [
                r
                for r in idx
                if (r["name"] or "").lower().startswith(low)
                or _slug_of(r["path"]).lower().startswith(low)
            ]

    for matches in step_matches():
        if len(matches) == 1:
            return matches[0]["id"], []
        if len(matches) > 1:
            live = [r for r in matches if not _is_archived_row(r)]
            if prefer_live and len(live) == 1:
                return live[0]["id"], []
            return None, _did_you_mean(
                idx, arg, _fts_name_desc(conn, arg, limit), limit, tied=matches
            )
    # 5. FTS over name/description; resolve iff a single candidate
    fts_ids = _fts_name_desc(conn, arg, limit)
    if len(fts_ids) == 1:
        return fts_ids[0], []
    # 6. unresolved: hand back "did you mean" suggestions
    return None, _did_you_mean(idx, arg, fts_ids, limit)


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
    if tier is not None:
        tier = normalize_tier(tier)
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


class TierMoveRefusedError(ValueError):
    """Spostamento di tier rifiutato prima di scrivere: la CLI lo traduce in exit code 2."""


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


def _refuse_taken_path(
    conn: sqlite3.Connection, row_id: int, new_path: str, action: str
) -> None:
    """Rifiuta lo spostamento se `new_path` e' gia' di un'altra riga.

    Va chiamata prima di `_retier_update`, che altrimenti fallirebbe sul vincolo UNIQUE
    di memories.path con un IntegrityError.
    """
    occupant = _db_get_by_path(conn, new_path)
    if occupant is None or occupant["id"] == row_id:
        return
    occ_id = occupant["id"]
    # una riga archiviata non si puo' spostare con retier (vedi `retier`): resta il prune
    remedy = "Prune" if _is_archived_row(occupant) else "Prune or retier"
    raise TierMoveRefusedError(
        f"cannot {action} id={row_id}: target path {new_path!r} is taken by memory "
        f"id={occ_id} (tier={occupant['tier']}); nothing changed. "
        f"{remedy} id={occ_id} first, then retry"
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
    Solleva TierMoveRefusedError, senza scrivere nulla, se il percorso di arrivo (quello
    originale per l'unarchive, archive/<tier>::<path> per l'archive) e' gia' occupato da
    un'altra riga.
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

        _refuse_taken_path(conn, row_id, new_path, "unarchive" if unarchive else "archive")
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
    Solleva TierMoveRefusedError, senza scrivere nulla, se la riga di partenza e'
    archiviata (il suo percorso con l'origine codificata diventerebbe un percorso vivo
    malformato: la ripristina `archive --unarchive`) o se il nuovo percorso e' gia'
    occupato da un'altra riga.
    """
    target_tier = normalize_tier(target_tier)
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
        if old_tier == ARCHIVE_TIER:
            raise TierMoveRefusedError(
                f"memory id={row_id} is archived: retier would write a malformed live path; "
                f"nothing changed. Use `mem archive --unarchive --id {row_id}` to restore it"
            )
        if old_tier == target_tier:
            raise ValueError(
                f"memory id={row_id} is already in tier {target_tier!r}"
            )

        new_path = _retiered_path(old_path or "", target_tier)
        _refuse_taken_path(conn, row_id, new_path, "retier")
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


# --- Compact -----------------------------------------------------------------


def _knn_unchanged(
    rowid: int, pre: list[tuple[int, float]], post: list[tuple[int, float]]
) -> bool:
    """True se dopo il rebuild `rowid` ha gli stessi vicini KNN (rowid, distanza) di prima
    e ritrova se stesso.

    A parita' di distanza l'ordine dei rowid non e' stabile (vettori identici) e il limite
    k puo' tagliare l'ultimo gruppo a pari distanza: si confrontano le distanze in ordine e
    le coppie (rowid, distanza) fuori dall'ultimo gruppo. L'identita' vale se `rowid` e'
    tra le righe a distanza zero, anche in pareggio con un gemello identico.
    """
    if not post or [d for _, d in pre] != [d for _, d in post]:
        return False
    last = post[-1][1]
    if {p for p in pre if p[1] != last} != {p for p in post if p[1] != last}:
        return False
    return any(r == rowid and d == 0 for r, d in post)


def compact_db(db_path=DB_PATH) -> dict:
    """Structural maintenance: FTS5 optimize + vec0 rebuild (reclaim dead chunk space) + VACUUM.

    Re-tier/upsert/prune churn fragments FTS and leaves dead vec0 slots that VACUUM alone can't repack.
    Letture iniziali (conteggi, vettori, KNN del campione), optimize FTS, rebuild vec0 e
    verifica (integrita', conteggi, nessun orfano, identita' KNN del campione) stanno in
    UNA sola transazione di scrittura (BEGIN IMMEDIATE), verificata PRIMA del COMMIT; il
    VACUUM gira solo dopo quel COMMIT (SQLite rifiuta il VACUUM dentro una transazione).
    Returns {'before_bytes','after_bytes','ok'}. Se la verifica fallisce fa ROLLBACK (DB
    invariato, nessun VACUUM) e solleva RuntimeError.
    """
    import os

    conn = _connect(db_path)
    conn.isolation_level = None
    try:
        before = os.path.getsize(db_path)

        def counts():
            return tuple(
                conn.execute(
                    "SELECT (SELECT COUNT(*) FROM memories),"
                    "(SELECT COUNT(*) FROM memories_fts),"
                    "(SELECT COUNT(*) FROM memories_vec)"
                ).fetchone()
            )

        def knn(rid):
            q = conn.execute(
                "SELECT embedding FROM memories_vec WHERE rowid=?", (rid,)
            ).fetchone()[0]
            return [
                (r[0], r[1])
                for r in conn.execute(
                    "SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k = ? "
                    "ORDER BY distance",
                    (q, k_of[rid]),
                ).fetchall()
            ]

        # le letture iniziali stanno nella stessa transazione di scrittura del rebuild:
        # un upsert concorrente non puo' cambiare un vettore tra la copia e il rebuild
        conn.execute("BEGIN IMMEDIATE")
        try:
            pre_counts = counts()
            data = [
                (r[0], r[1])
                for r in conn.execute(
                    "SELECT rowid, embedding FROM memories_vec ORDER BY rowid"
                ).fetchall()
            ]
            if not data:
                conn.execute("ROLLBACK")
                return {"before_bytes": before, "after_bytes": before, "ok": True}
            sample = [data[0][0], data[len(data) // 2][0], data[-1][0]]
            # k copre tutti i vettori identici a quello campionato, cosi' la riga stessa
            # resta sempre nel risultato anche quando ha dei gemelli a distanza zero
            vectors = dict(data)
            k_of = {
                r: max(3, sum(1 for emb in vectors.values() if emb == vectors[r]))
                for r in sample
            }
            pre_knn = {r: knn(r) for r in sample}

            # 1. FTS optimize
            conn.execute(
                "INSERT INTO memories_fts(memories_fts, rank) VALUES('integrity-check', 1)"
            )
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('optimize')")

            # 2. vec0 rebuild (preserves rowid = memories.id)
            conn.execute("DROP TABLE memories_vec")
            conn.execute(
                f"CREATE VIRTUAL TABLE memories_vec USING vec0("
                f"embedding float[{EMBED_DIM}], chunk_size={VEC_CHUNK_SIZE})"
            )
            conn.executemany(
                "INSERT INTO memories_vec(rowid, embedding) VALUES(?, ?)", data
            )

            # 3. verifica prima del COMMIT: se fallisce, il ROLLBACK lascia il DB com'era
            ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
            post_counts = counts()
            orphan = conn.execute(
                "SELECT COUNT(*) FROM memories m WHERE NOT EXISTS "
                "(SELECT 1 FROM memories_vec v WHERE v.rowid=m.id)"
            ).fetchone()[0]
            bad_knn = [r for r in sample if not _knn_unchanged(r, pre_knn[r], knn(r))]
            if ic != "ok" or post_counts != pre_counts or orphan or bad_knn:
                raise RuntimeError(
                    f"compact verification FAILED: integrity={ic} "
                    f"counts {pre_counts}->{post_counts} orphan={orphan} "
                    f"knn_mismatch={bad_knn}; rolled back, DB unchanged"
                )
            conn.execute("COMMIT")
        except Exception:
            # un errore SQLite grave puo' aver gia' annullato la transazione da solo
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        # 4. VACUUM, solo dopo il COMMIT verificato
        conn.execute("VACUUM")
        return {"before_bytes": before, "after_bytes": os.path.getsize(db_path), "ok": True}
    finally:
        conn.close()


# --- SQL di sola lettura -----------------------------------------------------

_SQL_LEAD_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def _check_readonly_sql(query: str) -> str:
    """Valida che `query` sia UNA sola istruzione SELECT/WITH e la restituisce pulita.

    Rifiuta istruzioni multiple separate da `;` (un solo `;` finale e' tollerato)
    e qualunque istruzione che non inizi con SELECT o WITH (PRAGMA, ATTACH, DML...).
    Il controllo sul `;` e' volutamente semplice: vale anche dentro i letterali.
    """
    q = (query or "").strip()
    if q.endswith(";"):
        q = q[:-1].rstrip()
    if not q:
        raise ValueError("empty SQL statement")
    if ";" in q:
        raise ValueError("multiple statements are not allowed (found ';')")
    if not _SQL_LEAD_RE.match(q):
        raise ValueError("only a single SELECT or WITH statement is allowed")
    return q


def _sql_authorizer(action, *_args):
    """Nega ATTACH, DETACH e PRAGMA anche se annidati in una SELECT."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def run_sql(query: str, db_path: Path | str = DB_PATH) -> tuple[list[str], list[tuple]]:
    """Esegue una SELECT/WITH su una connessione di sola lettura.

    Non carica l'estensione vec (le tabelle vec0 non sono interrogabili da qui).
    Restituisce (colonne, righe). Solleva ValueError se la query e' rifiutata,
    FileNotFoundError se il DB non esiste, sqlite3.Error per errori SQL.
    """
    q = _check_readonly_sql(query)
    p = assert_own_profile_path(Path(db_path).expanduser())
    if not p.is_file():
        raise FileNotFoundError(f"DB not found: {p}")
    conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.set_authorizer(_sql_authorizer)
        cur = conn.execute(q)
        cols = [d[0] for d in (cur.description or [])]
        return cols, [tuple(r) for r in cur.fetchall()]
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

    def _tier_or_die(value):
        """Normalizza un --tier della CLI; tier sconosciuto -> exit code 2."""
        if value is None:
            return None
        try:
            return normalize_tier(value)
        except ValueError as e:
            _die(str(e), 2)

    def _resolve_name_or_die(conn, arg, prefer_live=False):
        """Resolve --name via the resolution ladder; return the full row on a UNIQUE
        match. On an ambiguous/unknown name, print the 'did you mean' (id, name,
        slug, tier) suggestions to stderr (every tied row, plus other candidates up
        to the default limit) and exit 3. Never guesses.
        """
        row_id, suggestions = _resolve_name(conn, arg, prefer_live=prefer_live)
        if row_id is not None:
            return _db_get_by_id(conn, row_id)
        print(f"error: no memory uniquely matches name/slug {arg!r}", file=sys.stderr)
        if suggestions:
            print("did you mean:", file=sys.stderr)
            for s in suggestions:
                print(
                    f"  id={s['id']}  {s['name']}   (slug: {s['slug']}, tier: {s['tier']})",
                    file=sys.stderr,
                )
        sys.exit(3)

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
    p_search.add_argument(
        "query",
        nargs="+",
        help="Free-text search query. Pass several (space-separated, each quoted) "
        "for a multi-query UNION: 'q1' 'q2' 'q3'.",
    )
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
        help="Restrict results to this tier: instance, class, shared-projects, shared-global, archive (aliases: shared, global). "
        "Use 'archive' to query archived rows.",
    )
    p_search.add_argument(
        "--all",
        action="store_true",
        dest="include_archived",
        help="Include archive-tier rows (excluded from default recall).",
    )
    p_search.add_argument(
        "--realm",
        choices=REALM_FILTERS,
        default="all",
        help="Restrict results to this realm (default all).",
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
    p_upsert.add_argument(
        "--tier",
        required=True,
        help="Tier: instance, class, shared-projects, shared-global, archive (aliases: shared, global).",
    )
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
        help="Logical key path (default: <tier>/<agent-or-_>/<name>.md; if no row is there and "
        "exactly one live row has the same name, tier and agent (any agent for tier "
        "shared-global without --agent), that row's path and agent).",
    )
    p_upsert.add_argument(
        "--realm",
        choices=(REALM_PERSONAL, "untagged"),
        default=None,
        help=f"Realm tag. Omitted: a new row is personal iff {REALM_ENV_VAR}=personal, "
        "an existing row keeps its realm.",
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
    p_list.add_argument(
        "--tier", default=None, help="Filter by tier: instance, class, shared-projects, shared-global, archive (aliases: shared, global)."
    )
    p_list.add_argument(
        "--type",
        default=None,
        metavar="TYPE",
        dest="mem_type",
        help="Filter by type.",
    )
    p_list.add_argument(
        "--realm",
        choices=REALM_FILTERS,
        default="all",
        help="Filter by realm (default all).",
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
    p_stats = sub.add_parser(
        "stats",
        help="Row counts for memories / fts / vec, per-tier and per-type counts, last update.",
    )

    # compact
    p_compact = sub.add_parser(
        "compact",
        help="Structural maintenance: FTS5 optimize + vec0 rebuild + VACUUM (reclaims fragmentation; safe/verified).",
    )
    # SUPPRESS: senza, il default del sottoparser sovrascriveva il --db globale
    # e `--db <copia> compact` finiva sul DB live.
    p_compact.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help=f"DB path (default: {DB_PATH}).",
    )

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
        "--tier",
        default=None,
        help="Restrict candidates to this tier: instance, class, shared-projects, shared-global, archive (aliases: shared, global).",
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
        help="Target tier: instance, class, shared-projects, shared-global "
        "(aliases: shared, global). Rejects 'archive': use `archive`.",
    )

    # export
    p_export = sub.add_parser(
        "export",
        help="Dump a JSON manifest of all rows (backup / inspection).",
    )
    p_export.add_argument(
        "--with-body",
        action="store_true",
        help="Include each row's full body text (default: metadata only).",
    )

    # sql
    p_sql = sub.add_parser(
        "sql",
        help="Run ONE read-only SELECT/WITH statement (DB opened read-only, no vec extension).",
    )
    p_sql.add_argument("statement", metavar="SELECT", help="A single SELECT or WITH statement.")
    p_sql.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output a JSON array of objects instead of tab-separated rows.",
    )

    ns = parser.parse_args()

    # Un --db vuoto (variabile di shell non impostata) non deve ricadere sul DB di default
    if ns.db is not None and not ns.db.strip():
        _die("empty --db: pass a DB path or omit the option", 3)
    db = Path(ns.db).expanduser() if ns.db is not None else DB_PATH
    try:
        assert_own_profile_path(db)
    except CrossProfileDBError as e:
        _die(str(e), 3)

    if ns.cmd is None:
        parser.print_help()
        sys.exit(0)

    # Stesso muro di --db, prima di ogni accesso al disco e prima del controllo
    # sul DB mancante: un corpo letto dalla data home di un altro profilo
    # porterebbe i suoi dati in questo DB
    if ns.cmd == "upsert" and not ns.text_stdin:
        try:
            assert_own_profile_path(Path(ns.text_file).expanduser())
        except CrossProfileDBError as e:
            _die(f"--text-file refused: {e}", 3)

    # Un --db esplicito inesistente non va mai creato in silenzio (typo = DB vuoto).
    # Unico punto con l'elenco dei comandi esenti: init crea; in futuro anche
    # `sync restore`.
    if ns.db is not None and ns.cmd not in _DB_MAY_BE_MISSING_CMDS and not db.exists():
        _die(f"DB not found: {db}", 3)

    if ns.cmd == "init":
        init_db(db)
        print(f"init_db ok → {db}")

    elif ns.cmd == "search":
        # ns.query is a list (nargs="+"); search_many delegates to `search` for a
        # single query, so single-query behavior is unchanged.
        results = search_many(
            ns.query,
            k=ns.k,
            mode=ns.mode,
            db_path=db,
            include_archived=ns.include_archived,
            tier=_tier_or_die(ns.tier),
            realm=ns.realm,
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
            p = Path(ns.text_file).expanduser()
            if not p.exists():
                _die(f"--text-file not found: {p}")
            body = p.read_text(encoding="utf-8", errors="replace")

        ns.tier = _tier_or_die(ns.tier)
        agent_part = ns.agent or "_"
        default_path = f"{ns.tier}/{agent_part}/{ns.name}.md"
        logical_path = ns.path or default_path
        row_agent = ns.agent
        if ns.path is None:
            # Le skill salvano per nome senza --path: una riga unica con nome di file
            # diverso dal name va aggiornata, non duplicata (vedi _reusable_row)
            conn = _connect(db)
            try:
                reused = _reusable_row(conn, ns.tier, ns.agent, ns.name, default_path)
            finally:
                conn.close()
            if reused is not None:
                logical_path = reused["path"]
                # senza --agent la riga riusata conserva il proprio agent
                row_agent = reused["agent"]
                match = "name and tier" if _any_agent_reuse(ns.tier, ns.agent) else "name, tier and agent"
                # la riga riusata conserva il proprio realm: lo si segnala quando una riga
                # nuova di questa sessione ne avrebbe un altro
                realm_note = ""
                if ns.realm is None and reused["realm"] != _default_realm():
                    realm_note = f" realm={reused['realm'] or 'untagged'}"
                print(
                    f"note: no row at {default_path}; updating the only live row with this "
                    f"{match}: id={reused['id']} path={logical_path} agent={row_agent}"
                    f"{realm_note}",
                    file=sys.stderr,
                )

        row_id = upsert(
            path=logical_path,
            tier=ns.tier,
            agent=row_agent,
            type=ns.mem_type,
            name=ns.name,
            description=ns.description,
            text=body,
            db_path=db,
            realm=_UNSET if ns.realm is None else ns.realm,
        )
        print(f"upserted id={row_id}  path={logical_path}")

    elif ns.cmd == "get":
        conn = _connect(db)
        try:
            if ns.id is not None:
                row = _db_get_by_id(conn, ns.id)
            else:
                row = _resolve_name_or_die(conn, ns.name, prefer_live=True)
        finally:
            conn.close()

        if row is None:  # reachable only via --id (name path exits with suggestions)
            _die(f"memory not found: id={ns.id}")

        row_id = row["id"]
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
                params.append(_tier_or_die(ns.tier))
            if ns.mem_type:
                query_parts.append("AND type = ?")
                params.append(ns.mem_type)
            query_parts.append(f"AND {_realm_predicate(conn, ns.realm)}")
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
                row = _resolve_name_or_die(conn, ns.name)
        finally:
            conn.close()

        if row is None:  # reachable only via --id / --path
            ref = f"id={ns.id}" if ns.id is not None else f"path={ns.path!r}"
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
                row = _resolve_name_or_die(conn, ns.name, prefer_live=True)
        finally:
            conn.close()
        if row is None:  # reachable only via --id
            _die(f"memory not found: id={ns.id}")

        try:
            results = similar(
                row["id"], k=ns.k, tier=_tier_or_die(ns.tier), db_path=db
            )
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
                row = _resolve_name_or_die(conn, ns.name)
        finally:
            conn.close()
        if row is None:  # reachable only via --id
            _die(f"memory not found: id={ns.id}")

        try:
            info = archive(row["id"], unarchive=ns.unarchive, db_path=db)
        except TierMoveRefusedError as e:
            _die(str(e), 2)
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
                row = _resolve_name_or_die(conn, ns.name)
        finally:
            conn.close()
        if row is None:  # reachable only via --id
            _die(f"memory not found: id={ns.id}")

        try:
            info = retier(row["id"], _tier_or_die(ns.tier), db_path=db)
        except TierMoveRefusedError as e:
            _die(str(e), 2)
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
            by_tier = conn.execute(
                "SELECT COALESCE(tier, '(null)'), COUNT(*) FROM memories "
                "GROUP BY 1 ORDER BY 2 DESC, 1"
            ).fetchall()
            by_type = conn.execute(
                "SELECT COALESCE(type, '(null)'), COUNT(*) FROM memories "
                "GROUP BY 1 ORDER BY 2 DESC, 1"
            ).fetchall()
            last_updated = conn.execute("SELECT MAX(updated) FROM memories").fetchone()[0]
        finally:
            conn.close()

        print(f"memories : {mem_count}")
        print(f"fts rows : {fts_count}")
        print(f"vec rows : {vec_count}")
        # Righe aggiuntive (documentate in skills/mem-index): le prime tre restano invariate.
        print("tiers    : " + (", ".join(f"{t}={n}" for t, n in by_tier) or "-"))
        print("types    : " + (", ".join(f"{t}={n}" for t, n in by_type) or "-"))
        print(f"updated  : {last_updated or '-'}")

    elif ns.cmd == "compact":
        r = compact_db(db)
        print(
            f"compact ok={r['ok']}  {r['before_bytes']:,} -> {r['after_bytes']:,} bytes"
            f" ({100*(1-r['after_bytes']/r['before_bytes']):.0f}% smaller)"
        )

    elif ns.cmd == "export":
        # JSON manifest of all rows (backup / dump). Identity = `path`,
        # equality = content `hash` (sha256 of name+description+text, machine
        # independent), recency = `updated`. `id` is intentionally omitted
        # (machine-local); `created` and `realm` are included (a DB not yet migrated
        # reports realm null); `--with-body` adds `text`.
        conn = _connect(db)
        try:
            cols = "path, name, tier, type, agent, description, hash, created, updated, "
            cols += "realm" if _has_realm(conn) else "NULL AS realm"
            if ns.with_body:
                cols += ", text"
            rows = conn.execute(
                f"SELECT {cols} FROM memories ORDER BY path"
            ).fetchall()
        finally:
            conn.close()
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))

    elif ns.cmd == "sql":
        try:
            cols, rows = run_sql(ns.statement, db)
        except ValueError as e:
            _die(f"refused: {e}", 2)
        except (FileNotFoundError, sqlite3.Error) as e:
            _die(str(e))
        if ns.as_json:
            print(json.dumps(
                [dict(zip(cols, r)) for r in rows], ensure_ascii=False, indent=2,
                default=lambda b: f"<blob {len(b)}B>",
            ))
        else:
            def _cell(v) -> str:
                if v is None:
                    return ""
                if isinstance(v, bytes):
                    return f"<blob {len(v)}B>"
                return str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")

            print("\t".join(cols))
            for r in rows:
                print("\t".join(_cell(v) for v in r))
