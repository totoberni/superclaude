"""Formato e cifratura del vault di memoria (Stage A).

File chiave, derivazione HKDF, blob cifrati per riga, export dell'albero del vault,
restore in un DB nuovo e confronto tra due DB. Nessuna funzione tocca git o la rete:
il trasporto e' un modulo separato.

Contratto di sicurezza: nessun byte di chiave (grezzo, hex o base64) finisce in
eccezioni, repr, log o stderr. `cryptography` serve solo per cifrare e decifrare e si
importa al primo uso: senza il pacchetto la chiave si carica comunque, e lo stato a
livello di id (`key_matches`, `id_status`) funziona con la sola libreria standard.

Integrita' dell'albero: vault.json = {format, key_id, writer, rows, mac}, dove `mac` e'
l'HMAC-SHA256 (chiave k_mac) del manifesto {dir, format, key_id, writer, rows, blobs}, con
blobs = [id, sha256 del blob] ordinati per id e dir = la cartella del vault (sc o ds), che
non sta in vault.json ma lega il mac: chi legge la fornisce. Chi decifra un albero (restore,
export con base) lo verifica con `verify_tree` PRIMA di decifrare qualsiasi blob: un blob
rimpiazzato da uno piu' vecchio ma valido, uno tolto, uno in piu', un writer/rows modificato
o un albero presentato sotto l'altra cartella danno un rifiuto (exit 2). `id_status`
controlla il mac con `mac_matches` (mancante o errato: mac_ok False, senza sollevare); una
voce fuori forma nell'albero resta un errore di forma (exit 3). `read_vault_json` rifiuta
chiavi duplicate e byte non canonici (exit 3).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import zlib
from pathlib import Path

# memory_db sta accanto a questo file: serve anche quando il modulo e' caricato da percorso
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import memory_db as mdb  # noqa: E402

# --- Costanti di formato -----------------------------------------------------

# I NUL dell'intestazione fanno trattare il file come binario a rg/Grep, che lo saltano
KEY_HEADER = b"SMK1\x00\x00\x00\x00"
KEY_SIZE = 32
BLOB_MAGIC = b"SMV1"
KEY_ID_SIZE = 4
SIV_KEY_SIZE = 64
NAME_KEY_SIZE = 32
MAC_KEY_SIZE = 32
ROW_ID_HEX = 32
VAULT_FORMAT = "supermemdb/v1"
VAULT_DIRS = ("sc", "ds")
VAULT_JSON = "vault.json"
ROWS_DIR = "rows"
# Livello fisso: un blob riusato dalla base non cambia anche se cambia il livello
ZLIB_LEVEL = 9
# Campi autorevoli di una riga: tutto il resto (vettori, FTS, html, link) si ricostruisce
RECORD_FIELDS = (
    "agent", "created", "description", "name", "path",
    "realm", "text", "tier", "type", "updated",
)
CRYPTO_INSTALL_HINT = (
    "the 'cryptography' package is required to encrypt or decrypt the memory vault; "
    "install the pinned version into the shared venv: "
    "uv pip install --python <venv python> cryptography==50.0.2 "
    "(or run scripts/better_super_deps.py --pip-install)"
)

_HKDF_INFO_SIV = b"supermemdb/v1/siv"
_HKDF_INFO_NAME = b"supermemdb/v1/name"
_HKDF_INFO_ID = b"supermemdb/v1/id"
_HKDF_INFO_MAC = b"supermemdb/v1/mac"
_SIV_TAG_SIZE = 16
_BLOB_HEADER_SIZE = len(BLOB_MAGIC) + KEY_ID_SIZE
_ROW_ENTRY_RE = re.compile(rf"{ROWS_DIR}/([0-9a-f]{{{ROW_ID_HEX}}})\.bin\Z")
# Campi che il mac copre (con blobs); il mac stesso e' l'unico campo opzionale in lettura,
# cosi' un vault.json senza mac arriva a `verify_tree` e viene rifiutato con exit 2
_VAULT_META_KEYS = frozenset({"format", "key_id", "writer", "rows"})
_VAULT_MAC_FIELD = "mac"
_MAC_RE = re.compile(r"[0-9a-f]{64}")
_KEY_ID_RE = re.compile(rf"[0-9a-f]{{{2 * KEY_ID_SIZE}}}")
# writer = <etichetta>-<8 hex casuali> (R-1): niente spazi, maiuscole o caratteri di
# controllo, cosi' in vault.json non finiscono nomi host o utenti. Da usare con fullmatch.
WRITER_RE = re.compile(r"[a-z0-9][a-z0-9._]*-[0-9a-f]{8}")
# File accanto a un DB SQLite che il DB adotterebbe alla prima apertura
_SQLITE_SIDECARS = ("-journal", "-wal", "-shm")
# Campi che il DB tiene come TEXT annullabile e che il restore riscrive tali e quali
_NULLABLE_TEXT_FIELDS = ("agent", "description", "name", "type")


# --- Errori ------------------------------------------------------------------


class VaultError(Exception):
    """Errore del vault. Il messaggio non contiene mai byte di chiave; exit_code per la CLI."""

    exit_code = 3


class RefusedError(VaultError):
    """Operazione rifiutata per scelta (bersaglio non vuoto, chiave diversa): exit 2."""

    exit_code = 2


class KeyFileError(VaultError):
    """File chiave mancante, senza intestazione, di lunghezza errata o gia' esistente."""


class BlobAuthError(VaultError):
    """Blob non autentico: manomesso, key_id diverso, intestazione errata o AD diversa."""


class VaultFormatError(VaultError):
    """Albero, vault.json o testo in chiaro di forma non valida."""


class CryptoUnavailableError(VaultError, ImportError):
    """`cryptography` non e' installato: la CLI lo traduce in exit 3 con il suggerimento."""


# --- Derivazione delle chiavi ------------------------------------------------


def hkdf_sha256(ikm: bytes, *, info: bytes, length: int, salt: bytes = b"") -> bytes:
    """HKDF-SHA256 secondo RFC 5869; un salt vuoto vale HashLen byte a zero."""
    digest_size = hashlib.sha256().digest_size
    if not 0 < length <= 255 * digest_size:
        raise ValueError(f"HKDF length must be in 1..{255 * digest_size}, got {length}")
    prk = hmac.new(salt or bytes(digest_size), ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


class VaultKeys:
    """Chiavi derivate da una chiave master; la master non viene conservata.

    `key_id` e' pubblico (sta in vault.json e in ogni blob); le altre chiavi non
    compaiono mai in repr o messaggi.
    """

    __slots__ = ("_k_siv", "_k_name", "_k_mac", "key_id")

    def __init__(self, master: bytes) -> None:
        if not isinstance(master, (bytes, bytearray)) or len(master) != KEY_SIZE:
            raise KeyFileError(f"master key must be exactly {KEY_SIZE} bytes")
        master = bytes(master)
        self._k_siv = hkdf_sha256(master, info=_HKDF_INFO_SIV, length=SIV_KEY_SIZE)
        self._k_name = hkdf_sha256(master, info=_HKDF_INFO_NAME, length=NAME_KEY_SIZE)
        self._k_mac = hkdf_sha256(master, info=_HKDF_INFO_MAC, length=MAC_KEY_SIZE)
        self.key_id = hkdf_sha256(master, info=_HKDF_INFO_ID, length=KEY_ID_SIZE)

    def __repr__(self) -> str:
        return f"VaultKeys(key_id={self.key_id.hex()})"

    __str__ = __repr__

    def row_id(self, path: str) -> str:
        """Id di una riga: i primi 32 hex di HMAC-SHA256(k_name, path in UTF-8)."""
        digest = hmac.new(self._k_name, path.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest[:ROW_ID_HEX]

    def mac(self, message: bytes) -> str:
        """HMAC-SHA256(k_mac, message) in hex: autentica il manifesto di un albero."""
        return hmac.new(self._k_mac, message, hashlib.sha256).hexdigest()


# --- File chiave -------------------------------------------------------------


def load_keys(path: str | os.PathLike[str]) -> VaultKeys:
    """Legge il file chiave (intestazione SMK1 + 32 byte) e ne deriva le chiavi.

    Controlla intestazione e lunghezza; ogni errore cita solo il percorso e la lunghezza.
    """
    path = Path(path)
    expected = len(KEY_HEADER) + KEY_SIZE
    try:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            # un byte oltre il previsto basta a riconoscere un file troppo lungo
            data = handle.read(expected + 1)
    except FileNotFoundError:
        raise KeyFileError(f"key file not found: {path}") from None
    except OSError as exc:
        raise KeyFileError(f"cannot read key file {path}: {exc.strerror}") from None
    header_ok = data[:len(KEY_HEADER)] == KEY_HEADER
    master = data[len(KEY_HEADER):] if header_ok and len(data) == expected else None
    # Nessun byte di chiave resta tra le variabili locali del frame che solleva l'errore:
    # un renderer di traceback che stampa le locali non deve poterlo mostrare
    del data
    if not header_ok:
        raise KeyFileError(f"key file {path} has no valid SMK1 header")
    if master is None:
        raise KeyFileError(f"key file {path} has {size} bytes, expected {expected}")
    return VaultKeys(master)


def create_key_file(path: str | os.PathLike[str]) -> VaultKeys:
    """Crea un file chiave nuovo (intestazione + 32 byte casuali); mai sovrascrive.

    Pensato per un init successivo; un file gia' presente e' un rifiuto, non un errore da
    aggirare. La cartella deve esistere.
    """
    path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise KeyFileError(f"key file already exists and is never overwritten: {path}") from None
    master = secrets.token_bytes(KEY_SIZE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(KEY_HEADER + master)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # un file chiave a meta' non deve restare su disco
        path.unlink(missing_ok=True)
        raise
    return VaultKeys(master)


# --- Cifratura ---------------------------------------------------------------


def _crypto():
    """Importa al primo uso AESSIV e InvalidTag; senza pacchetto, errore con suggerimento."""
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESSIV
    except ImportError as exc:
        raise CryptoUnavailableError(CRYPTO_INSTALL_HINT) from exc
    return AESSIV, InvalidTag


def _check_vault_dir(vault_dir: str) -> None:
    if vault_dir not in VAULT_DIRS:
        raise VaultFormatError(f"vault dir must be one of {VAULT_DIRS}, got {vault_dir!r}")


def associated_data(vault_dir: str, row_id: str) -> list[bytes]:
    """AD di un blob: lega cartella, id e versione, cosi' i blob non si scambiano."""
    _check_vault_dir(vault_dir)
    return [f"{vault_dir}|{row_id}|v1".encode("ascii")]


def _canonical_dumps(value: object) -> bytes:
    """Serializzazione canonica comune a record e manifesto: chiavi ordinate, ASCII."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def canonical_json(record: dict) -> bytes:
    """JSON canonico dei 10 campi autorevoli: chiavi ordinate, separatori stretti, ASCII."""
    if set(record) != set(RECORD_FIELDS):
        raise VaultFormatError(f"record fields must be exactly {sorted(RECORD_FIELDS)}")
    return _canonical_dumps(record)


def seal(plaintext: bytes, keys: VaultKeys, vault_dir: str, row_id: str) -> bytes:
    """Blob = SMV1 + key_id + AES-SIV(k_siv) del JSON canonico compresso con zlib."""
    ad = associated_data(vault_dir, row_id)
    aessiv, _ = _crypto()
    payload = zlib.compress(plaintext, ZLIB_LEVEL)
    return BLOB_MAGIC + keys.key_id + aessiv(keys._k_siv).encrypt(payload, ad)


def open_blob(blob: bytes, keys: VaultKeys, vault_dir: str, row_id: str) -> bytes:
    """Verifica e decifra un blob; restituisce il JSON canonico (decompresso)."""
    ad = associated_data(vault_dir, row_id)
    if len(blob) < _BLOB_HEADER_SIZE + _SIV_TAG_SIZE or blob[:len(BLOB_MAGIC)] != BLOB_MAGIC:
        raise BlobAuthError(f"row {row_id}: not an SMV1 blob")
    blob_key_id = blob[len(BLOB_MAGIC):_BLOB_HEADER_SIZE]
    if blob_key_id != keys.key_id:
        raise BlobAuthError(
            f"row {row_id}: blob key_id {blob_key_id.hex()} does not match the key "
            f"(key_id {keys.key_id.hex()})"
        )
    aessiv, invalid_tag = _crypto()
    try:
        payload = aessiv(keys._k_siv).decrypt(blob[_BLOB_HEADER_SIZE:], ad)
    except invalid_tag:
        raise BlobAuthError(
            f"row {row_id}: authentication failed (tampered blob, other dir or other key)"
        ) from None
    try:
        return zlib.decompress(payload)
    except zlib.error:
        raise VaultFormatError(f"row {row_id}: payload is not zlib data") from None


def _decode_record(plaintext: bytes, keys: VaultKeys, row_id: str) -> dict:
    """Dal JSON canonico al record; rifiuta forme non canoniche e path che non danno l'id."""
    try:
        record = json.loads(plaintext.decode("ascii"))
    except ValueError:
        # nessun `from`: l'eccezione JSON porta con se' il testo in chiaro
        raise VaultFormatError(f"row {row_id}: plaintext is not canonical JSON") from None
    if not isinstance(record, dict) or set(record) != set(RECORD_FIELDS):
        raise VaultFormatError(f"row {row_id}: plaintext does not hold the record fields")
    if canonical_json(record) != plaintext:
        raise VaultFormatError(f"row {row_id}: plaintext is not in canonical form")
    if not isinstance(record["path"], str) or keys.row_id(record["path"]) != row_id:
        raise VaultFormatError(f"row {row_id}: record path does not match its id")
    # Stesso controllo dell'export: un record autentico ma non canonico (esportatore
    # vecchio o estraneo) fallisce qui, prima che il restore crei qualsiasi file
    if not _is_restorable(record):
        raise VaultFormatError(f"row {row_id}: record {_NOT_RESTORABLE}")
    return record


# --- vault.json --------------------------------------------------------------


def _check_writer(writer: object) -> None:
    """Il writer ha la forma <etichetta>-<8 hex>; il valore rifiutato non entra nel messaggio."""
    if not isinstance(writer, str) or WRITER_RE.fullmatch(writer) is None:
        raise VaultFormatError(
            "writer must have the form <label>-<8 lowercase hex>, label made of a-z, 0-9, "
            "'.' and '_'"
        )


def _render_vault_json(meta: dict) -> bytes:
    """Resa canonica di vault.json: unica fonte per chi lo scrive e per chi lo rilegge."""
    return (json.dumps(meta, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def vault_json(keys: VaultKeys, writer: str, tree: dict[str, bytes], vault_dir: str) -> bytes:
    """vault.json = {format, key_id, writer, rows, mac}, ASCII e deterministico.

    `tree` porta i blob rows/<id>.bin (un vault.json eventuale si ignora): `rows` e `mac`
    derivano da quei blob, cosi' il mac copre esattamente i byte che verranno pubblicati.
    `vault_dir` entra nel mac ma non in vault.json.
    """
    _check_writer(writer)
    meta = {
        "format": VAULT_FORMAT, "key_id": keys.key_id.hex(), "writer": writer,
        "rows": len(tree_row_ids(tree)),
    }
    meta[_VAULT_MAC_FIELD] = _manifest_mac(meta, tree, keys, vault_dir)
    return _render_vault_json(meta)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """object_pairs_hook: json.loads terrebbe l'ultimo valore di una chiave ripetuta."""
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise VaultFormatError("vault.json has duplicate keys")
    return dict(pairs)


def read_vault_json(data: bytes) -> dict:
    """Legge e valida la forma di vault.json (non la confronta con la chiave).

    Rifiuta chiavi duplicate e byte diversi dalla resa canonica di `vault_json`, cosi' chi
    legge i byte e chi legge i valori vedono lo stesso file. Il mac e' opzionale qui: un
    vault.json senza mac si rifiuta in `verify_tree` (exit 2), non come errore di forma.
    """
    try:
        meta = json.loads(data.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
    except ValueError:
        raise VaultFormatError("vault.json is not ASCII JSON") from None
    if not isinstance(meta, dict) or set(meta) - {_VAULT_MAC_FIELD} != _VAULT_META_KEYS:
        raise VaultFormatError(
            f"vault.json must hold the fields {sorted(_VAULT_META_KEYS | {_VAULT_MAC_FIELD})}"
        )
    if not all(isinstance(meta[key], str) for key in ("format", "key_id", "writer")):
        raise VaultFormatError("vault.json format, key_id and writer must be strings")
    if not isinstance(meta["rows"], int) or isinstance(meta["rows"], bool) or meta["rows"] < 0:
        raise VaultFormatError("vault.json rows must be a non-negative integer")
    # key_id e writer finiscono in messaggi e nello stato: solo forme note, senza caratteri
    # di controllo
    if _KEY_ID_RE.fullmatch(meta["key_id"]) is None:
        raise VaultFormatError(
            f"vault.json key_id must be {2 * KEY_ID_SIZE} lowercase hex characters"
        )
    _check_writer(meta["writer"])
    if data != _render_vault_json(meta):
        raise VaultFormatError("vault.json is not in canonical form")
    return meta


def key_matches(meta: dict, keys: VaultKeys) -> bool:
    """True se vault.json e' stato scritto con questa chiave (solo libreria standard)."""
    return meta["key_id"] == keys.key_id.hex()


def check_vault_meta(meta: dict, keys: VaultKeys) -> None:
    """Rifiuta (exit 2) un vault di altro formato o scritto con un'altra chiave.

    Guarda solo vault.json: chi legge un albero usa `verify_tree`, che aggiunge il mac.
    """
    if meta["format"] != VAULT_FORMAT:
        raise RefusedError(f"vault format {meta['format']!r} is not {VAULT_FORMAT!r}")
    if not key_matches(meta, keys):
        raise RefusedError(
            f"vault key_id {meta['key_id']!r} does not match the local key "
            f"(key_id {keys.key_id.hex()})"
        )


def _row_blobs(tree: dict[str, bytes]) -> dict[str, bytes]:
    """Blob rows/<id>.bin di un albero per id; ogni altra voce diversa da vault.json e' errore."""
    blobs = {}
    for rel, blob in tree.items():
        if rel == VAULT_JSON:
            continue
        match = _ROW_ENTRY_RE.match(rel)
        if match is None:
            raise VaultFormatError(f"unexpected entry in the vault tree: {rel!r}")
        blobs[match.group(1)] = blob
    return blobs


def tree_row_ids(tree: dict[str, bytes]) -> set[str]:
    """Id delle righe di un albero; ogni voce diversa da vault.json e rows/<id>.bin e' errore."""
    return set(_row_blobs(tree))


def _manifest_mac(meta: dict, tree: dict[str, bytes], keys: VaultKeys, vault_dir: str) -> str:
    """Mac atteso: HMAC del manifesto {dir, format, key_id, writer, rows, blobs} dell'albero.

    blobs = [id, sha256(blob) hex] ordinati per id. Un blob rimpiazzato da uno piu' vecchio
    ma valido (stessa AD), uno tolto o uno in piu' cambiano il manifesto, quindi il mac; dir
    lega il mac alla cartella, cosi' un albero sc presentato come ds non verifica.
    """
    _check_vault_dir(vault_dir)
    manifest = {field: meta[field] for field in _VAULT_META_KEYS}
    manifest["dir"] = vault_dir
    manifest["blobs"] = [
        [row_id, hashlib.sha256(blob).hexdigest()]
        for row_id, blob in sorted(_row_blobs(tree).items())
    ]
    return keys.mac(_canonical_dumps(manifest))


def mac_matches(meta: dict, tree: dict[str, bytes], keys: VaultKeys, vault_dir: str) -> bool:
    """True se il mac di vault.json e' quello dell'albero; mancante o malformato vale False.

    Solo libreria standard. Il confronto e' a tempo costante e il mac non entra mai in un
    messaggio.
    """
    mac = meta.get(_VAULT_MAC_FIELD)
    if not isinstance(mac, str) or _MAC_RE.fullmatch(mac) is None:
        return False
    expected = _manifest_mac(meta, tree, keys, vault_dir)
    return hmac.compare_digest(mac.encode("ascii"), expected.encode("ascii"))


def verify_tree(meta: dict, tree: dict[str, bytes], keys: VaultKeys, vault_dir: str) -> None:
    """Rifiuta (exit 2) formato o chiave diversi e un mac mancante, malformato o errato.

    Da chiamare su ogni albero letto PRIMA di decifrare un blob: i blob da soli non provano
    che l'insieme sia quello scritto dal writer (rollback di una riga, riga tolta) ne' che
    sia della cartella `vault_dir` richiesta.
    """
    check_vault_meta(meta, keys)
    if not mac_matches(meta, tree, keys, vault_dir):
        raise RefusedError(
            "vault.json mac is missing, malformed or does not match the tree "
            "(a blob was replaced, added or removed, vault.json was edited, or the tree "
            "belongs to the other vault dir)"
        )


# --- Lettura dei DB ----------------------------------------------------------


def read_records(db: str | os.PathLike[str]) -> list[dict]:
    """Righe del DB come record dei 10 campi autorevoli, ordinate per path (sola lettura)."""
    conn = mdb._connect(db, readonly=True)
    try:
        realm = "realm" if mdb._has_realm(conn) else "NULL AS realm"
        rows = conn.execute(
            "SELECT path, tier, agent, type, name, description, text, created, updated, "
            f"{realm} FROM memories ORDER BY path"
        ).fetchall()
    finally:
        conn.close()
    return [{field: row[field] for field in RECORD_FIELDS} for row in rows]


def _read_paths(db: str | os.PathLike[str]) -> list[str]:
    conn = mdb._connect(db, readonly=True)
    try:
        return [row[0] for row in conn.execute("SELECT path FROM memories")]
    finally:
        conn.close()


def _count_links(db: str | os.PathLike[str]) -> int:
    conn = mdb._connect(db, readonly=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    finally:
        conn.close()


_NOT_RESTORABLE = (
    "cannot round-trip: path, text, agent, type, name, description, tier, realm, "
    "created or updated is not in canonical form"
)


def _is_restorable(record: dict) -> bool:
    """True se upsert_many riscriverebbe la riga identica.

    upsert_many normalizza tier e realm, mette "adesso" al posto di un timestamp mancante
    e solleva eccezioni fuori da VaultError su tipi inattesi (NULL, BLOB, numeri).
    """
    try:
        return (
            isinstance(record["path"], str)
            and isinstance(record["text"], str)
            and all(record[field] is None or isinstance(record[field], str)
                    for field in _NULLABLE_TEXT_FIELDS)
            # None passa _validate_timestamp come None: qui vale come non ripristinabile
            and mdb._validate_timestamp(record["created"], "created") is not None
            and mdb._validate_timestamp(record["updated"], "updated") is not None
            and isinstance(record["tier"], str)
            and mdb.normalize_tier(record["tier"]) == record["tier"]
            and mdb._normalize_realm(record["realm"]) == record["realm"]
        )
    except ValueError:
        return False


def _check_restorable(record: dict) -> None:
    """Rifiuta in export una riga che il restore non riscriverebbe identica."""
    if not _is_restorable(record):
        raise VaultFormatError(f"row {record['path']!r} {_NOT_RESTORABLE}")


# --- Export ------------------------------------------------------------------


def export_tree(
    db: str | os.PathLike[str],
    base: dict[str, bytes] | None,
    *,
    keys: VaultKeys,
    vault_dir: str,
    writer: str,
) -> dict[str, bytes]:
    """Albero del vault (percorso relativo alla cartella del vault -> byte).

    Contiene vault.json e rows/<id>.bin per ogni riga del DB. `base` e' l'albero gia'
    pubblicato, con la stessa forma (o None): un suo blob si riusa byte per byte quando
    decifra allo stesso JSON canonico. La base si verifica come ogni albero letto (formato,
    chiave, mac) prima di decifrare qualsiasi blob; un blob di base non autentico interrompe
    l'export. I link non si esportano: derivano dal testo.
    """
    _check_vault_dir(vault_dir)
    _check_writer(writer)
    base = base or {}
    if VAULT_JSON in base:
        # Una base scritta con un'altra chiave non darebbe mai id comuni: l'export la
        # sostituirebbe in silenzio con un albero intero sotto un key_id nuovo; una base
        # manomessa (blob rimpiazzato o tolto) non deve diventare la base di un push
        verify_tree(read_vault_json(base[VAULT_JSON]), base, keys, vault_dir)
    elif tree_row_ids(base):
        # Senza vault.json la base non e' verificabile: i suoi blob non si riusano in silenzio
        raise VaultFormatError("base holds row blobs but no vault.json")
    rows: dict[str, bytes] = {}
    for record in read_records(db):
        _check_restorable(record)
        row_id = keys.row_id(record["path"])
        rel = f"{ROWS_DIR}/{row_id}.bin"
        if rel in rows:
            raise VaultFormatError(f"row id collision on {row_id}")
        plaintext = canonical_json(record)
        old = base.get(rel)
        if old is not None and open_blob(old, keys, vault_dir, row_id) == plaintext:
            rows[rel] = old
        else:
            rows[rel] = seal(plaintext, keys, vault_dir, row_id)
    tree = {VAULT_JSON: vault_json(keys, writer, rows, vault_dir)}
    tree.update(sorted(rows.items()))
    return tree


def id_status(
    db: str | os.PathLike[str], tree: dict[str, bytes], *, keys: VaultKeys, vault_dir: str
) -> dict:
    """Stato senza decifrare (senza cryptography): chiave, mac e id da aggiungere o togliere.

    `mac_ok` e' True/False solo con la chiave giusta (senza non si puo' verificare: None);
    un mac mancante o errato non solleva, cosi' lo stato resta leggibile. Una voce fuori
    forma nell'albero (nome diverso da rows/<id>.bin) resta un errore di forma (exit 3).
    """
    meta = read_vault_json(tree[VAULT_JSON]) if VAULT_JSON in tree else None
    local = {keys.row_id(path) for path in _read_paths(db)}
    remote = tree_row_ids(tree)
    key_match = None if meta is None else key_matches(meta, keys)
    return {
        "key_match": key_match,
        "mac_ok": mac_matches(meta, tree, keys, vault_dir) if key_match else None,
        "remote_writer": None if meta is None else meta["writer"],
        "local_rows": len(local),
        "remote_rows": len(remote),
        "would_add": len(local - remote),
        "would_remove": len(remote - local),
    }


# --- Restore -----------------------------------------------------------------


def _memory_rows(path: Path) -> int | None:
    """Righe in `memories`, o None se il file non e' un DB di memoria leggibile.

    Apertura immutable: un DB vuoto in modalita' WAL e senza sidecar non deve generare
    -wal/-shm (una connessione ro non sa rimuoverli e il restore poi rifiuterebbe). E'
    sicuro perche' `_refuse_unless_empty` controlla i sidecar prima di chiamarla. Il muro tra
    profili si applica qui, come in `mdb._connect`, perche' la funzione apre SQLite da se';
    l'estensione sqlite-vec non serve a contare le righe.
    """
    mdb.assert_own_profile_path(path)
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return None
    try:
        return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _refuse_unless_empty(target: Path) -> None:
    """Il bersaglio deve mancare, essere un file vuoto o un DB di memoria senza righe.

    In ogni caso nessun sidecar SQLite: anche con il bersaglio mancante, un -wal rimasto da
    un DB spostato altrove verrebbe adottato dal DB ripristinato alla prima apertura.
    """
    for suffix in _SQLITE_SIDECARS:
        if Path(f"{target}{suffix}").exists():
            raise RefusedError(
                f"restore target {target} has a {suffix} file (writer active, or left by a "
                "database moved aside): remove it first"
            )
    if not target.exists():
        return
    if not target.is_file():
        raise RefusedError(f"restore target {target} exists and is not a file")
    if target.stat().st_size == 0:
        return
    if _memory_rows(target) != 0:
        raise RefusedError(
            f"restore target {target} is not empty: restore only writes into an empty DB"
        )


def _decrypt_tree(tree: dict[str, bytes], keys: VaultKeys, vault_dir: str) -> list[dict]:
    """Verifica vault.json e mac, poi decifra ogni blob in ordine di id; si ferma al primo."""
    if VAULT_JSON not in tree:
        raise VaultFormatError("vault tree has no vault.json")
    meta = read_vault_json(tree[VAULT_JSON])
    verify_tree(meta, tree, keys, vault_dir)
    ids = tree_row_ids(tree)
    if meta["rows"] != len(ids):
        raise VaultFormatError(
            f"vault.json lists {meta['rows']} rows but the tree holds {len(ids)} blobs"
        )
    return [
        _decode_record(open_blob(tree[f"{ROWS_DIR}/{row_id}.bin"], keys, vault_dir, row_id),
                       keys, row_id)
        for row_id in sorted(ids)
    ]


def _write_new_db(partial: Path, records: list[dict], embed: bool) -> int:
    """Crea il DB in `partial` con un'unica transazione; restituisce il numero di link."""
    try:
        mdb.init_db(partial)
        conn = mdb._connect(partial)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            mdb.upsert_many(records, conn, embed=embed)
            conn.execute("COMMIT")
            links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        finally:
            conn.close()
    except BaseException:
        partial.unlink(missing_ok=True)
        Path(f"{partial}-journal").unlink(missing_ok=True)
        raise
    return links


def restore(
    tree: dict[str, bytes],
    new_db: str | os.PathLike[str],
    *,
    keys: VaultKeys,
    vault_dir: str,
    embed: bool = True,
) -> dict:
    """Decifra l'albero in un DB nuovo: rows e link ricostruiti, vettori se `embed`.

    Rifiuta un bersaglio non vuoto e un `<db>.partial` rimasto da un restore interrotto.
    Verifica formato, chiave e mac dell'albero (`verify_tree`) prima di decifrare qualsiasi
    blob. Decifra tutto prima di scrivere e si ferma al primo blob non autentico senza
    lasciare file; poi scrive `<db>.partial` (upsert_many in una transazione, embedding a lotti),
    chiude e lo rinomina con os.replace. Restituisce {"rows", "links"}.
    """
    _check_vault_dir(vault_dir)
    target = Path(new_db).expanduser()
    mdb.assert_own_profile_path(target)
    partial = target.with_name(target.name + ".partial")
    _refuse_unless_empty(target)
    if partial.exists():
        raise RefusedError(f"{partial} exists (interrupted restore?): remove it first")
    records = _decrypt_tree(tree, keys, vault_dir)
    links = _write_new_db(partial, records, embed)
    try:
        _refuse_unless_empty(target)
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return {"rows": len(records), "links": links}


# --- Confronto ---------------------------------------------------------------


def _is_newer(local: str | None, vault: str | None) -> bool:
    """True se `updated` locale e' piu' recente; un valore mancante o non canonico non lo e'.

    Un `updated` senza offset (writer legacy o SQL diretto) non si confronta con uno
    +00:00: la riga resta tra le differenze, con `updated` tra i campi.
    """
    try:
        if (mdb._validate_timestamp(local, "updated") is None
                or mdb._validate_timestamp(vault, "updated") is None):
            return False
    except ValueError:
        return False
    return datetime.datetime.fromisoformat(local) > datetime.datetime.fromisoformat(vault)


def compare(vault_db: str | os.PathLike[str], local_db: str | os.PathLike[str]) -> dict:
    """Confronta il DB ripristinato dal vault con il DB locale, campo per campo.

    Tre elenchi separati, senza mai riportare testo di riga:
    - differences: stessa path, campi autorevoli diversi e `updated` locale non piu' recente;
    - newer_local: path solo nel locale, o con `updated` locale piu' recente (dopo il push);
    - removed_after_push: path solo nel vault (potate o archiviate dopo il push).
    """
    vault = {record["path"]: record for record in read_records(vault_db)}
    local = {record["path"]: record for record in read_records(local_db)}
    differences, newer_local = [], sorted(local.keys() - vault.keys())
    for path in sorted(vault.keys() & local.keys()):
        fields = [field for field in RECORD_FIELDS if vault[path][field] != local[path][field]]
        if not fields:
            continue
        if _is_newer(local[path]["updated"], vault[path]["updated"]):
            newer_local.append(path)
        else:
            differences.append({"path": path, "fields": fields})
    return {
        "compared": len(local),
        "vault_rows": len(vault),
        "differences": differences,
        "newer_local": sorted(newer_local),
        "removed_after_push": sorted(vault.keys() - local.keys()),
        "links": {"vault": _count_links(vault_db), "local": _count_links(local_db)},
    }
