"""Test del formato del vault (memvault): file chiave, HKDF, blob, export, restore, compare.

Solo chiavi fittizie generate in cartelle temporanee e DB sintetici minuscoli costruiti con
memory_db. Gli embedding sono vettori deterministici finti: nessun modello viene caricato.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import traceback
import zlib

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import memory_db as mdb  # noqa: E402
import memvault as mv  # noqa: E402

_T1 = "2026-01-01T00:00:00+00:00"
_T2 = "2026-02-01T10:20:30+00:00"
_T3 = "2026-03-01T00:00:00.250000+00:00"
_WRITER = "testbox-0123abcd"
_ALPHA = "instance/meta/alpha.md"
_BETA = "shared-projects/_/beta.md"
_GAMMA = "archive/shared-global::shared-global/_/gamma.md"


def _rows():
    return [
        {"path": _ALPHA, "tier": "instance", "agent": "meta", "type": "feedback",
         "name": "alpha", "description": "first note",
         "text": "Alpha body links [[beta]] and [[gamma]].\nSecond line caffè.",
         "created": _T1, "updated": _T2, "realm": "personal"},
        {"path": _BETA, "tier": "shared-projects", "agent": None, "type": "project",
         "name": "beta", "description": "", "text": "Beta body points back to [[alpha]].",
         "created": _T1, "updated": _T3, "realm": None},
        {"path": _GAMMA, "tier": "archive", "agent": "w-doc", "type": "reference",
         "name": "gamma", "description": "archived row",
         "text": "Gamma paragraph. " * 30 + "☃ snowman",
         "created": _T2, "updated": _T2, "realm": None},
    ]


def _fake_vectors(texts):
    """Vettori deterministici di EMBED_DIM valori ricavati dallo sha256 del testo."""
    out = []
    for text in texts:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        out.append([b / 255.0 for b in (seed * 12)[:mdb.EMBED_DIM]])
    return out


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setattr(mdb, "_embed_batch", _fake_vectors)
    monkeypatch.delenv(mdb.REALM_ENV_VAR, raising=False)


@pytest.fixture
def key_file(tmp_path):
    folder = tmp_path / "keydir"
    folder.mkdir()
    path = folder / "dummy.key"
    mv.create_key_file(path)
    return path


@pytest.fixture
def keys(key_file):
    return mv.load_keys(key_file)


def _build_db(path, rows):
    mdb.init_db(path)
    conn = mdb._connect(path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        mdb.upsert_many(rows, conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return path


def _export(db, keys, base=None, vault_dir="sc"):
    return mv.export_tree(db, base, keys=keys, vault_dir=vault_dir, writer=_WRITER)


def _query(db, sql):
    conn = mdb._connect(db, readonly=True)
    try:
        return [tuple(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def _link_pairs(db):
    return sorted(_query(
        db, "SELECT m.path, l.dst_name FROM links l JOIN memories m ON m.id = l.src_id"))


def _secrets_of(key_file, keys):
    """Materiale segreto da cercare nei messaggi: chiave master e chiavi derivate."""
    master = key_file.read_bytes()[len(mv.KEY_HEADER):]
    return [master, keys._k_siv, keys._k_name, keys._k_mac]


def _forms(secret):
    """Forme in cui un segreto potrebbe trapelare: intero e finestre, grezzo, hex, base64."""
    forms = [secret, secret.hex().encode(), secret.hex().upper().encode(),
             repr(secret)[2:-1].encode()]
    for encoder in (base64.b64encode, base64.urlsafe_b64encode):
        full = encoder(secret)
        forms += [full, full.rstrip(b"=")]
        forms += [full[i:i + 12] for i in range(0, len(full) - 12, 4)]
    forms += [secret[i:i + 8] for i in range(len(secret) - 7)]
    forms += [secret[i:i + 8].hex().encode() for i in range(len(secret) - 7)]
    # finestre nella forma repr: un prefisso della chiave dentro un bytes stampato
    forms += [repr(secret[i:i + 8])[2:-1].encode() for i in range(len(secret) - 7)]
    return forms


def _assert_no_leak(data, secrets):
    if isinstance(data, str):
        blobs = [data.encode("utf-8", "backslashreplace"), data.encode("latin-1", "ignore")]
    else:
        blobs = [data]
    for secret in secrets:
        for form in _forms(secret):
            for blob in blobs:
                assert form not in blob, "secret material found in error output"


def _exception_text(exc):
    return "".join(traceback.format_exception(exc)) + repr(exc) + str(exc)


def _master_of(key_file):
    return key_file.read_bytes()[len(mv.KEY_HEADER):]


def _ref_mac(master, meta, blobs, vault_dir="sc"):
    """Mac del vault calcolato senza memvault: HKDF di cryptography, JSON scritto a mano.

    `meta` ha format, key_id, writer, rows; `blobs` e' {id: byte del blob}; `vault_dir` e'
    la cartella che il mac lega (non sta in vault.json).
    """
    k_mac = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                 info=b"supermemdb/v1/mac").derive(master)
    listing = ",".join(f'["{row_id}","{hashlib.sha256(blob).hexdigest()}"]'
                       for row_id, blob in sorted(blobs.items()))
    manifest = (f'{{"blobs":[{listing}],"dir":"{vault_dir}","format":"{meta["format"]}",'
                f'"key_id":"{meta["key_id"]}","rows":{meta["rows"]},'
                f'"writer":"{meta["writer"]}"}}')
    return hmac.new(k_mac, manifest.encode("ascii"), hashlib.sha256).hexdigest()


def _tree_blobs(tree):
    """{id: blob} dei rows/<id>.bin di un albero."""
    return {rel[len("rows/"):-len(".bin")]: blob for rel, blob in tree.items()
            if rel.startswith("rows/")}


def _resigned(tree, keys, writer=_WRITER, vault_dir="sc"):
    """Albero con vault.json rifatto e mac valido sui blob attuali (per `vault_dir`).

    Rappresenta chi ha la chiave e scrive blob difettosi: il test raggiunge cosi' lo strato
    sotto il mac (autenticita' del blob, forma del record) invece di fermarsi al mac.
    """
    return dict(tree, **{"vault.json": mv.vault_json(keys, writer, tree, vault_dir)})


def _edit_meta(tree, drop=(), **changes):
    """Albero con vault.json modificato a mano, SENZA chiave: il mac resta quello vecchio."""
    meta = json.loads(tree["vault.json"].decode("ascii"))
    meta.update(changes)
    for field in drop:
        del meta[field]
    raw = (json.dumps(meta, sort_keys=True, indent=2) + "\n").encode("ascii")
    return dict(tree, **{"vault.json": raw})


def _hand_signed(tree, master, **changes):
    """Come `_edit_meta`, ma con il mac ricalcolato a mano: un writer con la chiave e difetti."""
    meta = json.loads(_edit_meta(tree, **changes)["vault.json"].decode("ascii"))
    return _edit_meta(tree, mac=_ref_mac(master, meta, _tree_blobs(tree)), **changes)


# --- HKDF e derivazione ------------------------------------------------------

_RFC5869 = [
    # Test Case 1, 2 e 3 della RFC 5869 (SHA-256)
    (bytes([0x0B]) * 22, bytes(range(0x00, 0x0D)), bytes(range(0xF0, 0xFA)), 42,
     "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
     "34007208d5b887185865"),
    (bytes(range(0x00, 0x50)), bytes(range(0x60, 0xB0)), bytes(range(0xB0, 0x100)), 82,
     "b11e398dc80327a1c8e7f78c596a49344f012eda2d4efad8a050cc4c19afa97c"
     "59045a99cac7827271cb41c65e590e09da3275600c2f09b8367793a9aca3db71"
     "cc30c58179ec3e87c14c01d5c1f3434f1d87"),
    (bytes([0x0B]) * 22, b"", b"", 42,
     "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d"
     "9d201395faa4b61a96c8"),
]


@pytest.mark.parametrize("ikm, salt, info, length, okm", _RFC5869)
def test_hkdf_rfc5869_test_vectors(ikm, salt, info, length, okm):
    assert mv.hkdf_sha256(ikm, salt=salt, info=info, length=length).hex() == okm


def test_hkdf_rejects_out_of_range_length():
    for length in (0, 255 * 32 + 1):
        with pytest.raises(ValueError):
            mv.hkdf_sha256(b"x", info=b"", length=length)


def test_vault_keys_match_an_independent_hkdf():
    master = bytes(range(32))
    keys = mv.VaultKeys(master)

    def ref(info, length):
        return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(master)

    assert keys._k_siv == ref(b"supermemdb/v1/siv", 64)
    assert keys._k_name == ref(b"supermemdb/v1/name", 32)
    assert keys._k_mac == ref(b"supermemdb/v1/mac", 32)
    assert keys._k_mac.hex() == (
        "c803d45ae6701c88927c56a0ac5ff9c6005e0be9ebb521d73f1f80e74e6c43fc")  # anche con openssl
    assert keys.key_id == ref(b"supermemdb/v1/id", 4)
    k_name = ref(b"supermemdb/v1/name", 32)
    for path in ("instance/meta/a.md", "shared-projects/_/caffè.md"):
        expected = hmac.new(k_name, path.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        assert keys.row_id(path) == expected
        assert len(expected) == 32


def test_vault_keys_repr_shows_only_key_id(key_file, keys):
    text = repr(keys) + str(keys) + f"{keys}"
    assert keys.key_id.hex() in text
    _assert_no_leak(text, _secrets_of(key_file, keys))


def test_vault_keys_refuse_wrong_master_length():
    for master in (b"", bytes(31), bytes(33)):
        with pytest.raises(mv.KeyFileError):
            mv.VaultKeys(master)


# --- File chiave -------------------------------------------------------------


def test_create_key_file_writes_header_plus_key_and_never_overwrites(tmp_path):
    path = tmp_path / "dummy.key"
    created = mv.create_key_file(path)
    data = path.read_bytes()
    assert len(data) == 40
    assert data[:8] == b"SMK1\x00\x00\x00\x00"
    assert b"\x00" in data  # i NUL dell'intestazione rendono il file binario per rg/Grep
    assert mv.load_keys(path).key_id == created.key_id
    with pytest.raises(mv.KeyFileError, match="never overwritten"):
        mv.create_key_file(path)
    assert path.read_bytes() == data


def test_two_dummy_keys_differ(tmp_path):
    first = mv.create_key_file(tmp_path / "one.key")
    second = mv.create_key_file(tmp_path / "two.key")
    assert first.key_id != second.key_id


def _bad_key_files(master):
    return {
        "missing header": master,
        "zeroed header": bytes(8) + master,
        "short key": mv.KEY_HEADER + master[:31],
        "long key": mv.KEY_HEADER + master + b"\x01",
        "header only": mv.KEY_HEADER,
    }


@pytest.mark.parametrize("case", ["missing header", "zeroed header", "short key",
                                  "long key", "header only"])
def test_bad_key_files_are_refused_without_key_bytes(tmp_path, case):
    master = os.urandom(32)
    path = tmp_path / "bad.key"
    path.write_bytes(_bad_key_files(master)[case])
    with pytest.raises(mv.KeyFileError) as info:
        mv.load_keys(path)
    _assert_no_leak(_exception_text(info.value), [master])


def test_missing_key_file_is_refused(tmp_path):
    with pytest.raises(mv.KeyFileError, match="not found"):
        mv.load_keys(tmp_path / "absent.key")


def _load_error_with_locals(path):
    """Traceback con le variabili locali, come lo stamperebbe un renderer che le cattura.

    L'errore si cattura qui, cosi' il frame del test (che conosce la chiave) resta fuori.
    """
    try:
        mv.load_keys(path)
    except mv.KeyFileError as exc:
        rendered = traceback.TracebackException.from_exception(exc, capture_locals=True)
        return "".join(rendered.format())
    raise AssertionError("expected a KeyFileError")


@pytest.mark.parametrize("case", ["missing header", "zeroed header", "short key",
                                  "long key", "header only"])
def test_bad_key_file_tracebacks_with_locals_carry_no_key_bytes(tmp_path, case):
    master = os.urandom(32)
    path = tmp_path / "bad.key"
    path.write_bytes(_bad_key_files(master)[case])
    text = _load_error_with_locals(path)
    assert "load_keys" in text and "KeyFileError" in text
    _assert_no_leak(text, [master])


def test_long_key_file_reports_its_real_size(tmp_path):
    path = tmp_path / "bad.key"
    path.write_bytes(mv.KEY_HEADER + os.urandom(32) + bytes(100))
    with pytest.raises(mv.KeyFileError, match="has 140 bytes, expected 40"):
        mv.load_keys(path)


# --- JSON canonico, AD e blob ------------------------------------------------


def _record():
    return {"type": "feedback", "updated": _T2, "tier": "instance",
            "text": "caffè ☃ \"q\"\n", "realm": "personal",
            "path": "instance/meta/n.md", "name": "n", "description": "d",
            "created": _T1, "agent": None}


def test_canonical_json_known_answer():
    expected = (
        b'{"agent":null,"created":"2026-01-01T00:00:00+00:00","description":"d",'
        b'"name":"n","path":"instance/meta/n.md","realm":"personal",'
        b'"text":"caff\\u00e8 \\u2603 \\"q\\"\\n","tier":"instance","type":"feedback",'
        b'"updated":"2026-02-01T10:20:30+00:00"}'
    )
    assert mv.canonical_json(_record()) == expected


def test_canonical_json_refuses_missing_or_extra_fields():
    short = _record()
    del short["agent"]
    extra = dict(_record(), hash="x")
    for record in (short, extra):
        with pytest.raises(mv.VaultFormatError):
            mv.canonical_json(record)


def test_associated_data_known_answer():
    row_id = "0123456789abcdef0123456789abcdef"
    assert mv.associated_data("sc", row_id) == [b"sc|0123456789abcdef0123456789abcdef|v1"]
    assert mv.associated_data("ds", row_id) == [b"ds|0123456789abcdef0123456789abcdef|v1"]
    for bad in ("xx", "SC", "", "sc/"):
        with pytest.raises(mv.VaultFormatError):
            mv.associated_data(bad, row_id)


def test_blob_layout_and_ad_binding(keys):
    record = _record()
    row_id = keys.row_id(record["path"])
    plaintext = mv.canonical_json(record)
    blob = mv.seal(plaintext, keys, "sc", row_id)
    ad = [f"sc|{row_id}|v1".encode("ascii")]
    expected = b"SMV1" + keys.key_id + AESSIV(keys._k_siv).encrypt(
        zlib.compress(plaintext, 9), ad)
    assert blob == expected
    assert mv.open_blob(blob, keys, "sc", row_id) == plaintext
    # un'AD spezzata in tre parti non e' la stessa AD
    with pytest.raises(InvalidTag):
        AESSIV(keys._k_siv).decrypt(blob[8:], [b"sc", row_id.encode("ascii"), b"v1"])
    with pytest.raises(mv.BlobAuthError):
        mv.open_blob(blob, keys, "ds", row_id)
    with pytest.raises(mv.BlobAuthError):
        mv.open_blob(blob, keys, "sc", keys.row_id("instance/meta/other.md"))


# --- Export ------------------------------------------------------------------


def test_vault_json_shape(tmp_path, keys):
    tree = _export(_build_db(tmp_path / "a.db", _rows()), keys)
    raw = tree["vault.json"]
    raw.decode("ascii")
    meta = mv.read_vault_json(raw)
    mac = meta.pop("mac")
    assert re.fullmatch(r"[0-9a-f]{64}", mac)
    assert meta == {"format": "supermemdb/v1", "key_id": keys.key_id.hex(),
                    "writer": _WRITER, "rows": 3}
    assert sorted(tree) == sorted(["vault.json"] + [
        f"rows/{keys.row_id(row['path'])}.bin" for row in _rows()])
    assert all(blob[:4] == b"SMV1" and blob[4:8] == keys.key_id
               for rel, blob in tree.items() if rel != "vault.json")


@pytest.mark.parametrize("raw", [
    b"not json", b"[]", b'{"format":"supermemdb/v1","key_id":"00","writer":"w"}',
    b'{"format":"supermemdb/v1","key_id":"00","writer":"w","rows":-1}',
    b'{"format":"supermemdb/v1","key_id":"00","writer":"w","rows":true}',
    b'{"format":"supermemdb/v1","key_id":"00","writer":"w","rows":1,"x":1}',
    '{"format":"supermemdb/v1","key_id":"00","writer":"wè","rows":1}'.encode("utf-8"),
])
def test_read_vault_json_rejects_bad_shapes(raw):
    with pytest.raises(mv.VaultFormatError):
        mv.read_vault_json(raw)


@pytest.mark.parametrize("key_id, writer", [
    ("\x1b[2J\x1b[31mfake", _WRITER),
    ("ABCDEF01", _WRITER),
    ("0123abc", _WRITER),
    ("0123abcd0", _WRITER),
    ("0123abcd", "host DESKTOP-ABC user jdoe"),
    ("0123abcd", "win11"),
    ("0123abcd", "win11-0123ABCD"),
    ("0123abcd", "Win11-0123abcd"),
    ("0123abcd", "win11-0123abcd\n"),
    ("0123abcd", "win11-0123abcd\x1b[2J"),
])
def test_read_vault_json_rejects_malformed_key_id_and_writer(key_id, writer):
    raw = json.dumps({"format": mv.VAULT_FORMAT, "key_id": key_id, "writer": writer,
                      "rows": 0}).encode("ascii")
    with pytest.raises(mv.VaultFormatError) as info:
        mv.read_vault_json(raw)
    assert "\x1b" not in str(info.value)


def test_vault_json_refuses_a_free_form_writer(tmp_path, keys):
    db = _build_db(tmp_path / "a.db", _rows())
    for writer in ("host DESKTOP-ABC user jdoe", "win11", "win11-0123abc", "WIN11-0123abcd",
                   "win11-0123abcd ", None):
        with pytest.raises(mv.VaultFormatError):
            mv.vault_json(keys, writer, {}, "sc")
        with pytest.raises(mv.VaultFormatError):
            mv.export_tree(db, None, keys=keys, vault_dir="sc", writer=writer)
    for writer in ("win11-0123abcd", "box.2_x-ffffffff"):
        assert mv.read_vault_json(mv.vault_json(keys, writer, {}, "sc"))["writer"] == writer


def test_refused_error_escapes_a_foreign_key_id(keys):
    meta = {"format": mv.VAULT_FORMAT, "key_id": "\x1b[2J\x1b[31mfake",
            "writer": _WRITER, "rows": 0}
    with pytest.raises(mv.RefusedError) as info:
        mv.check_vault_meta(meta, keys)
    assert "\x1b" not in str(info.value)


def test_reexport_gives_identical_bytes(tmp_path, keys):
    db = _build_db(tmp_path / "a.db", _rows())
    first = _export(db, keys)
    assert _export(db, keys) == first
    assert _export(db, keys, base=first) == first
    # il mac e' deterministico e la base riusata lo verifica prima di essere riusata
    mv.verify_tree(mv.read_vault_json(first["vault.json"]), first, keys, "sc")


def test_blob_reuse_across_zlib_level_change(tmp_path, keys, monkeypatch):
    db = _build_db(tmp_path / "a.db", _rows())
    monkeypatch.setattr(mv, "ZLIB_LEVEL", 1)
    base = _export(db, keys)
    monkeypatch.setattr(mv, "ZLIB_LEVEL", 9)
    fresh = _export(db, keys)
    row_keys = [rel for rel in base if rel != "vault.json"]
    assert all(fresh[rel] != base[rel] for rel in row_keys)  # il livello cambia i byte
    assert _export(db, keys, base=base) == base  # ma i blob di base si riusano
    # una riga cambiata riceve un blob nuovo, le altre restano quelle di base
    changed = dict(_rows()[1], text="Beta body rewritten, still pointing to [[alpha]].")
    mdb.upsert(db_path=db, **changed)
    after = _export(db, keys, base=base)
    beta = f"rows/{keys.row_id(_BETA)}.bin"
    assert after[beta] != base[beta]
    assert mv.open_blob(after[beta], keys, "sc", keys.row_id(_BETA)) == mv.canonical_json(
        mv.read_records(db)[[r["path"] for r in mv.read_records(db)].index(_BETA)])
    assert all(after[rel] == base[rel] for rel in row_keys if rel != beta)


def test_export_refuses_a_tampered_base_blob(tmp_path, keys):
    db = _build_db(tmp_path / "a.db", _rows())
    base = _export(db, keys)
    rel = f"rows/{keys.row_id(_ALPHA)}.bin"
    base[rel] = base[rel][:-1] + bytes([base[rel][-1] ^ 0x01])
    # con il mac rifatto il blob arriva alla verifica di autenticita' e interrompe l'export
    with pytest.raises(mv.BlobAuthError):
        _export(db, keys, base=_resigned(base, keys))


def test_export_refuses_a_base_written_with_another_key_or_format(tmp_path, keys):
    db = _build_db(tmp_path / "a.db", _rows())
    foreign = _export(db, mv.VaultKeys(os.urandom(32)))
    with pytest.raises(mv.RefusedError, match="key_id") as info:
        _export(db, keys, base=foreign)
    assert info.value.exit_code == 2
    other_format = _export(db, keys)
    other_format["vault.json"] = other_format["vault.json"].replace(
        b"supermemdb/v1", b"supermemdb/v2")
    with pytest.raises(mv.RefusedError, match="format"):
        _export(db, keys, base=other_format)


def test_blobs_hold_no_plaintext(tmp_path, keys):
    rows = _rows()
    tree = _export(_build_db(tmp_path / "a.db", rows), keys)
    blobs = b"".join(blob for rel, blob in tree.items() if rel != "vault.json")
    for row in rows:
        for field in ("text", "name", "description", "path", "agent"):
            data = (row[field] or "").encode("utf-8")
            if not data:
                continue
            windows = [data] if len(data) <= 8 else [
                data[i:i + 8] for i in range(len(data) - 7)]
            assert not any(window in blobs for window in windows), field
            assert data not in tree["vault.json"]


def test_export_refuses_rows_that_cannot_round_trip(tmp_path, keys):
    for column, value in (("updated", "2026-01-01 00:00:00"), ("created", None),
                          ("tier", "shared"), ("realm", "Personal")):
        db = _build_db(tmp_path / f"bad-{column}.db", _rows())
        conn = mdb._connect(db)
        try:
            conn.execute(f"UPDATE memories SET {column} = ? WHERE path = ?", (value, _BETA))
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(mv.VaultFormatError, match="cannot round-trip"):
            _export(db, keys)


@pytest.mark.parametrize("column, value", [
    ("path", None), ("name", b"blob"), ("description", b"blob"), ("agent", b"blob"),
    ("type", b"blob"), ("tier", b"shared-projects"),
])
def test_export_refuses_null_path_and_blob_columns_as_vault_errors(
        tmp_path, keys, column, value):
    db = _build_db(tmp_path / "a.db", _rows())
    conn = mdb._connect(db)
    try:
        conn.execute(f"UPDATE memories SET {column} = ? WHERE path = ?", (value, _BETA))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(mv.VaultFormatError, match="cannot round-trip") as info:
        _export(db, keys)
    assert info.value.exit_code == 3


def test_export_refuses_unknown_vault_dir_and_empty_writer(tmp_path, keys):
    db = _build_db(tmp_path / "a.db", _rows())
    with pytest.raises(mv.VaultFormatError):
        _export(db, keys, vault_dir="xx")
    with pytest.raises(mv.VaultFormatError):
        mv.export_tree(db, None, keys=keys, vault_dir="sc", writer="")


# --- Integrita' dell'albero (mac) --------------------------------------------

_KAT_MASTER = bytes(range(32))
_KAT_BLOBS = {
    "0123456789abcdef0123456789abcdef": b"first blob bytes",
    "fedcba9876543210fedcba9876543210": b"second blob bytes",
}
_MAC_CASES = ["missing", "empty", "short", "long", "uppercase", "not hex", "int", "list",
              "zeros", "flipped", "flipped last"]
# Valori calcolati con codice indipendente (HKDF e HMAC della sola stdlib, manifesto scritto
# a mano); il manifesto contiene "dir", quindi sc e ds danno mac diversi
_KAT_MACS = {
    "sc": "0f654ac3cab26015dd0aec006f823a49166789b21152bdd5f4a649f9b004d5b6",
    "ds": "9bd40453b03998cf19688154bf70c970d3c5073b97e32e189ffef697dd61865c",
}


@pytest.mark.parametrize("vault_dir", ["sc", "ds"])
def test_vault_json_mac_known_answer(vault_dir):
    keys = mv.VaultKeys(_KAT_MASTER)
    tree = {f"rows/{row_id}.bin": blob for row_id, blob in _KAT_BLOBS.items()}
    meta = mv.read_vault_json(mv.vault_json(keys, "testbox-0123abcd", tree, vault_dir))
    assert meta["rows"] == 2
    assert meta["mac"] == _KAT_MACS[vault_dir]
    assert meta["mac"] == _ref_mac(_KAT_MASTER, meta, _KAT_BLOBS, vault_dir)


def test_export_writes_the_mac_over_the_exact_output_blobs(tmp_path, key_file, keys):
    tree = _export(_build_db(tmp_path / "a.db", _rows()), keys)
    meta = mv.read_vault_json(tree["vault.json"])
    assert meta["mac"] == _ref_mac(_master_of(key_file), meta, _tree_blobs(tree))
    assert len(_tree_blobs(tree)) == meta["rows"] == 3
    assert mv.mac_matches(meta, tree, keys, "sc") is True
    mv.verify_tree(meta, tree, keys, "sc")


def _rolled_back_tree(tmp_path, keys):
    """(albero con UNA riga tornata al blob precedente, albero attuale, db).

    Il blob vecchio e' autentico (stessa chiave, stessa AD): da solo nessun controllo sul
    blob lo distingue.
    """
    db = _build_db(tmp_path / "a.db", _rows())
    old = _export(db, keys)
    mdb.upsert(db_path=db, **dict(_rows()[1], text="Beta rewritten after the first push."))
    current = _export(db, keys, base=old)
    beta = f"rows/{keys.row_id(_BETA)}.bin"
    assert current[beta] != old[beta]
    return dict(current, **{beta: old[beta]}), current, db


def test_restore_refuses_a_single_blob_rolled_back_to_an_older_valid_blob(tmp_path, keys):
    rolled, current, _ = _rolled_back_tree(tmp_path, keys)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(mv.RefusedError, match="mac") as info:
        mv.restore(rolled, out / "memory.db", keys=keys, vault_dir="sc", embed=False)
    assert info.value.exit_code == 2
    assert list(out.iterdir()) == []
    # controllo positivo: lo stesso albero con il blob attuale e' accettato
    summary = mv.restore(current, out / "memory.db", keys=keys, vault_dir="sc", embed=False)
    assert summary["rows"] == 3


def test_restore_refuses_a_deleted_blob_with_or_without_rows_edited(tmp_path, keys):
    tree = _export(_build_db(tmp_path / "a.db", _rows()), keys)
    gone = _row_rels(tree)[0]
    deleted = {rel: blob for rel, blob in tree.items() if rel != gone}
    for bad in (deleted, _edit_meta(deleted, rows=2)):
        with pytest.raises(mv.RefusedError, match="mac") as info:
            mv.restore(bad, tmp_path / "x.db", keys=keys, vault_dir="sc", embed=False)
        assert info.value.exit_code == 2
    assert not (tmp_path / "x.db").exists()


def test_restore_refuses_a_stray_blob_with_or_without_rows_edited(tmp_path, keys):
    db = _build_db(tmp_path / "a.db", _rows())
    full = _export(db, keys)
    beta = f"rows/{keys.row_id(_BETA)}.bin"
    mdb._prune_row(_query(db, f"SELECT id FROM memories WHERE path = '{_BETA}'")[0][0], db)
    pruned = _export(db, keys)
    assert beta not in pruned
    # il blob di una riga potata ricompare nell'albero: e' autentico ma non e' piu' del vault
    resurrected = dict(pruned, **{beta: full[beta]})
    for bad in (resurrected, _edit_meta(resurrected, rows=3)):
        with pytest.raises(mv.RefusedError, match="mac") as info:
            mv.restore(bad, tmp_path / "x.db", keys=keys, vault_dir="sc", embed=False)
        assert info.value.exit_code == 2
    assert not (tmp_path / "x.db").exists()


@pytest.mark.parametrize("changes", [{"writer": "other-0badc0de"}, {"rows": 2}, {"rows": 4}])
def test_restore_refuses_an_edited_writer_or_rows(tmp_path, keys, changes):
    tree = _export(_build_db(tmp_path / "a.db", _rows()), keys)
    with pytest.raises(mv.RefusedError, match="mac") as info:
        mv.restore(_edit_meta(tree, **changes), tmp_path / "x.db", keys=keys,
                   vault_dir="sc", embed=False)
    assert info.value.exit_code == 2
    assert not (tmp_path / "x.db").exists()


def _tree_with_mac(tree, case):
    real = mv.read_vault_json(tree["vault.json"])["mac"]
    if case == "missing":
        return _edit_meta(tree, drop=("mac",))
    values = {"empty": "", "short": real[:-1], "long": real + "0", "uppercase": real.upper(),
              "not hex": "g" * 64, "int": 5, "list": [real], "zeros": "0" * 64,
              "flipped": ("1" if real[0] != "1" else "2") + real[1:],
              # solo l'ultimo carattere: un confronto di un prefisso del mac non lo vede
              "flipped last": real[:-1] + ("1" if real[-1] != "1" else "2")}
    return _edit_meta(tree, mac=values[case])


@pytest.mark.parametrize("case", _MAC_CASES)
def test_a_missing_or_bad_mac_is_refused_by_restore_verify_and_export(tmp_path, keys, case):
    db = _build_db(tmp_path / "a.db", _rows())
    bad = _tree_with_mac(_export(db, keys), case)
    with pytest.raises(mv.RefusedError, match="mac") as info:
        mv.restore(bad, tmp_path / "x.db", keys=keys, vault_dir="sc", embed=False)
    assert info.value.exit_code == 2
    assert not (tmp_path / "x.db").exists()
    with pytest.raises(mv.RefusedError, match="mac"):
        mv.verify_tree(mv.read_vault_json(bad["vault.json"]), bad, keys, "sc")
    with pytest.raises(mv.RefusedError, match="mac"):
        _export(db, keys, base=bad)


def test_the_mac_is_checked_before_any_blob_is_decrypted(tmp_path, keys, monkeypatch):
    rolled, _, db = _rolled_back_tree(tmp_path, keys)

    def boom(*args, **kwargs):
        raise AssertionError("a blob was decrypted before the mac was verified")

    monkeypatch.setattr(mv, "open_blob", boom)
    with pytest.raises(mv.RefusedError):
        mv.restore(rolled, tmp_path / "x.db", keys=keys, vault_dir="sc", embed=False)
    with pytest.raises(mv.RefusedError):
        _export(db, keys, base=rolled)


def test_id_status_reports_mac_ok_instead_of_raising(tmp_path, keys):
    rolled, current, db = _rolled_back_tree(tmp_path, keys)
    assert mv.id_status(db, current, keys=keys, vault_dir="sc")["mac_ok"] is True
    status = mv.id_status(db, rolled, keys=keys, vault_dir="sc")
    assert status["mac_ok"] is False and status["key_match"] is True
    deleted = {rel: blob for rel, blob in current.items() if rel != _row_rels(current)[0]}
    assert mv.id_status(db, deleted, keys=keys, vault_dir="sc")["mac_ok"] is False
    for case in _MAC_CASES:
        status = mv.id_status(db, _tree_with_mac(current, case), keys=keys, vault_dir="sc")
        assert status["mac_ok"] is False
    # senza la chiave giusta o senza vault.json il mac non si puo' verificare
    other = mv.id_status(db, current, keys=mv.VaultKeys(os.urandom(32)), vault_dir="sc")
    assert other["key_match"] is False and other["mac_ok"] is None
    assert mv.id_status(db, {}, keys=keys, vault_dir="sc")["mac_ok"] is None


def test_mac_refusals_carry_no_key_bytes_and_not_the_expected_mac(tmp_path, key_file, keys):
    rolled, _, _ = _rolled_back_tree(tmp_path, keys)
    meta = mv.read_vault_json(rolled["vault.json"])
    expected = _ref_mac(_master_of(key_file), meta, _tree_blobs(rolled))
    assert expected != meta["mac"]
    exc = _raised(mv.restore, rolled, tmp_path / "x.db", keys)
    assert isinstance(exc, mv.RefusedError)
    rendered = traceback.TracebackException.from_exception(exc, capture_locals=True)
    text = _exception_text(exc) + "".join(rendered.format())
    _assert_no_leak(text, _secrets_of(key_file, keys))
    # il mac atteso per un albero manomesso e' proprio cio' che servirebbe per falsificarlo
    assert expected not in text


@pytest.mark.parametrize("built, presented", [("sc", "ds"), ("ds", "sc")])
def test_the_mac_binds_the_vault_dir(tmp_path, key_file, keys, built, presented):
    db = _build_db(tmp_path / "a.db", _rows())
    tree = _export(db, keys, vault_dir=built)
    meta = mv.read_vault_json(tree["vault.json"])
    assert meta["mac"] == _ref_mac(_master_of(key_file), meta, _tree_blobs(tree), built)
    assert meta["mac"] != _ref_mac(_master_of(key_file), meta, _tree_blobs(tree), presented)
    assert "dir" not in meta  # la cartella lega il mac ma non sta in vault.json
    mv.verify_tree(meta, tree, keys, built)
    with pytest.raises(mv.RefusedError, match="mac") as info:
        mv.verify_tree(meta, tree, keys, presented)
    assert info.value.exit_code == 2
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(mv.RefusedError, match="mac") as info:
        mv.restore(tree, out / "memory.db", keys=keys, vault_dir=presented, embed=False)
    assert info.value.exit_code == 2
    assert list(out.iterdir()) == []
    with pytest.raises(mv.RefusedError, match="mac"):
        _export(db, keys, base=tree, vault_dir=presented)
    assert mv.id_status(db, tree, keys=keys, vault_dir=built)["mac_ok"] is True
    assert mv.id_status(db, tree, keys=keys, vault_dir=presented)["mac_ok"] is False
    # lo stesso albero si ripristina e si riesporta byte per byte sotto la sua cartella
    assert mv.restore(tree, out / "memory.db", keys=keys, vault_dir=built, embed=False)["rows"] == 3
    assert _export(db, keys, base=tree, vault_dir=built) == tree


def test_the_mac_refuses_an_unknown_vault_dir(tmp_path, keys):
    tree = _export(_build_db(tmp_path / "a.db", _rows()), keys)
    meta = mv.read_vault_json(tree["vault.json"])
    for bad in ("xx", "SC", ""):
        with pytest.raises(mv.VaultFormatError):
            mv.vault_json(keys, _WRITER, tree, bad)
        with pytest.raises(mv.VaultFormatError):
            mv.mac_matches(meta, tree, keys, bad)
        with pytest.raises(mv.VaultFormatError):
            mv.verify_tree(meta, tree, keys, bad)


def _canonical_vault_json(tmp_path, keys):
    return _export(_build_db(tmp_path / "a.db", _rows()), keys)["vault.json"]


def test_read_vault_json_refuses_duplicate_keys(tmp_path, keys):
    raw = _canonical_vault_json(tmp_path, keys)
    zeros = ("0" * 64).encode("ascii")
    # chiave ripetuta: json.loads terrebbe l'ultima, quindi il mac buono maschererebbe il primo
    for key, value in ((b"mac", b'"' + zeros + b'"'), (b"rows", b"99"), (b"writer", b'"x-00000000"')):
        dup = raw.replace(b"{\n", b'{\n  "' + key + b'": ' + value + b",\n", 1)
        assert dup != raw
        with pytest.raises(mv.VaultFormatError, match="duplicate") as info:
            mv.read_vault_json(dup)
        assert info.value.exit_code == 3
    assert mv.read_vault_json(raw)["mac"]


def test_read_vault_json_refuses_non_canonical_bytes(tmp_path, keys):
    raw = _canonical_vault_json(tmp_path, keys)
    meta = json.loads(raw.decode("ascii"))
    variants = {
        "compact": json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("ascii"),
        "unsorted": (json.dumps(dict(reversed(list(meta.items()))), indent=2)
                     + "\n").encode("ascii"),
        "no final newline": raw[:-1],
        "crlf": raw.replace(b"\n", b"\r\n"),
        "double newline": raw + b"\n",
        "tab indent": raw.replace(b"  ", b"\t"),
        "leading space": b" " + raw,
        "escaped letter": raw.replace(b'"rows"', b'"\\u0072ows"'),
    }
    for name, data in variants.items():
        assert data != raw, name
        with pytest.raises(mv.VaultFormatError, match="canonical") as info:
            mv.read_vault_json(data)
        assert info.value.exit_code == 3, name
    assert mv.read_vault_json(raw) == meta
    # la resa canonica e' quella che scrive vault_json, anche senza mac
    no_mac = {key: value for key, value in meta.items() if key != "mac"}
    assert mv.read_vault_json(mv._render_vault_json(no_mac)) == no_mac


def test_a_non_canonical_or_duplicate_vault_json_stops_restore_and_export(tmp_path, keys):
    tree = _export(_build_db(tmp_path / "a.db", _rows()), keys)
    compact = dict(tree, **{"vault.json": json.dumps(
        json.loads(tree["vault.json"]), sort_keys=True, separators=(",", ":")).encode("ascii")})
    dup = dict(tree, **{"vault.json": tree["vault.json"].replace(
        b"{\n", b'{\n  "mac": "' + b"0" * 64 + b'",\n', 1)})
    db = tmp_path / "a.db"
    for bad in (compact, dup):
        with pytest.raises(mv.VaultFormatError) as info:
            mv.restore(bad, tmp_path / "x.db", keys=keys, vault_dir="sc", embed=False)
        assert info.value.exit_code == 3
        with pytest.raises(mv.VaultFormatError):
            _export(db, keys, base=bad)
        with pytest.raises(mv.VaultFormatError):
            mv.id_status(db, bad, keys=keys, vault_dir="sc")
    assert not (tmp_path / "x.db").exists()


def test_id_status_tells_a_bad_mac_from_a_malformed_entry(tmp_path, keys):
    # un mac mancante o errato e' mac_ok False; una voce fuori forma resta un errore di forma
    db = _build_db(tmp_path / "a.db", _rows())
    tree = _export(db, keys)
    assert mv.id_status(db, _tree_with_mac(tree, "missing"), keys=keys,
                        vault_dir="sc")["mac_ok"] is False
    for entry in ("rows/" + "A" * 32 + ".bin", "rows/../vault.json", "notes.txt"):
        with pytest.raises(mv.VaultFormatError) as info:
            mv.id_status(db, dict(tree, **{entry: b"x"}), keys=keys, vault_dir="sc")
        assert info.value.exit_code == 3


def test_memory_rows_applies_the_profile_wall_before_opening_sqlite(tmp_path, monkeypatch):
    db = _build_db(tmp_path / "a.db", _rows())
    seen = []

    def wall(path, profile=None):
        seen.append(path)
        raise mdb.CrossProfileDBError("cross-profile access refused (stub)")

    def boom(*args, **kwargs):
        raise AssertionError("sqlite was opened before the profile wall")

    monkeypatch.setattr(mdb, "assert_own_profile_path", wall)
    monkeypatch.setattr(sqlite3, "connect", boom)
    with pytest.raises(mdb.CrossProfileDBError):
        mv._memory_rows(db)
    assert seen == [db]


def test_export_refuses_a_base_with_row_blobs_but_no_vault_json(tmp_path, keys):
    db = _build_db(tmp_path / "a.db", _rows())
    base = _export(db, keys)
    without_meta = {rel: blob for rel, blob in base.items() if rel != "vault.json"}
    with pytest.raises(mv.VaultFormatError, match="vault.json") as info:
        _export(db, keys, base=without_meta)
    assert info.value.exit_code == 3
    with pytest.raises(mv.VaultFormatError):
        _export(db, keys, base={"notes.txt": b"x"})
    # una base vuota o assente resta valida
    assert _export(db, keys, base={}) == base
    assert _export(db, keys, base=None) == base


# --- Restore -----------------------------------------------------------------


def test_round_trip_every_field_and_links(tmp_path, keys, monkeypatch):
    src = _build_db(tmp_path / "src.db", _rows())
    tree = _export(src, keys)
    target = tmp_path / "restored" / "memory.db"
    replaced = []
    real_replace = os.replace

    def spy(a, b):
        replaced.append((str(a), str(b)))
        return real_replace(a, b)

    monkeypatch.setattr(os, "replace", spy)
    summary = mv.restore(tree, target, keys=keys, vault_dir="sc")
    assert replaced == [(str(target) + ".partial", str(target))]
    assert not (tmp_path / "restored" / "memory.db.partial").exists()
    assert mv.read_records(target) == mv.read_records(src)
    assert {r["path"]: r for r in mv.read_records(target)}[_ALPHA]["realm"] == "personal"
    assert _query(target, "SELECT path, hash FROM memories ORDER BY path") == _query(
        src, "SELECT path, hash FROM memories ORDER BY path")
    assert _link_pairs(target) == _link_pairs(src)
    assert len(_link_pairs(src)) == 3
    assert summary == {"rows": 3, "links": 3}
    assert _query(target, "SELECT COUNT(*) FROM memories_vec") == [(3,)]
    conn = mdb._connect(target)
    try:
        conn.execute("INSERT INTO memories_fts(memories_fts, rank) VALUES('integrity-check', 1)")
    finally:
        conn.close()
    report = mv.compare(target, src)
    assert report["differences"] == []
    assert report["newer_local"] == [] and report["removed_after_push"] == []
    assert report["compared"] == report["vault_rows"] == 3
    assert report["links"] == {"vault": 3, "local": 3}


def test_restore_without_embedding_writes_no_vectors(tmp_path, keys, monkeypatch):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)

    def boom(texts):
        raise AssertionError("embed=False must not call the model")

    monkeypatch.setattr(mdb, "_embed_batch", boom)
    target = tmp_path / "drill.db"
    assert mv.restore(tree, target, keys=keys, vault_dir="sc", embed=False) == {
        "rows": 3, "links": 3}
    assert _query(target, "SELECT COUNT(*) FROM memories_vec") == [(0,)]


def test_restore_accepts_a_zero_byte_or_schema_only_target(tmp_path, keys):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    empty_file = tmp_path / "zero.db"
    empty_file.write_bytes(b"")
    schema_only = tmp_path / "schema.db"
    mdb.init_db(schema_only)
    for target in (empty_file, schema_only):
        assert mv.restore(tree, target, keys=keys, vault_dir="sc", embed=False)["rows"] == 3
        assert len(mv.read_records(target)) == 3


def _empty_wal_db(path):
    """DB di memoria senza righe, in modalita' WAL e senza sidecar (chiuso da chi l'ha creato)."""
    mdb.init_db(path)
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        conn.close()
    return path


def _sidecar_names(path):
    return sorted(suffix for suffix in mv._SQLITE_SIDECARS
                  if os.path.exists(f"{path}{suffix}"))


def test_restore_accepts_an_empty_wal_mode_target_without_sidecars(tmp_path, keys):
    src = _build_db(tmp_path / "src.db", _rows())
    tree = _export(src, keys)
    target = _empty_wal_db(tmp_path / "wal.db")
    assert _sidecar_names(target) == []
    # contare le righe non deve creare -wal/-shm: il secondo controllo li rifiuterebbe
    assert mv._memory_rows(target) == 0
    assert _sidecar_names(target) == []
    summary = mv.restore(tree, target, keys=keys, vault_dir="sc", embed=False)
    assert summary["rows"] == 3
    assert mv.read_records(target) == mv.read_records(src)
    assert _sidecar_names(target) == []
    assert not os.path.exists(f"{target}.partial")


def test_restore_refuses_a_non_empty_target(tmp_path, keys):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    busy = _build_db(tmp_path / "busy.db", [_rows()[0]])
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database, just bytes")
    folder = tmp_path / "folder.db"
    folder.mkdir()
    for target in (busy, junk, folder):
        before = target.read_bytes() if target.is_file() else None
        with pytest.raises(mv.RefusedError) as info:
            mv.restore(tree, target, keys=keys, vault_dir="sc", embed=False)
        assert info.value.exit_code == 2
        assert (target.read_bytes() if target.is_file() else None) == before
        assert not (tmp_path / (target.name + ".partial")).exists()


def test_restore_refuses_a_leftover_partial(tmp_path, keys):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    partial = tmp_path / "new.db.partial"
    partial.write_bytes(b"left over")
    with pytest.raises(mv.RefusedError, match="partial"):
        mv.restore(tree, tmp_path / "new.db", keys=keys, vault_dir="sc", embed=False)
    assert partial.read_bytes() == b"left over"
    assert not (tmp_path / "new.db").exists()


_STALE = b"sidecar left behind by a database that was moved aside"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_restore_refuses_a_stale_sidecar_next_to_a_missing_target(tmp_path, keys, suffix):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    out = tmp_path / "out"
    out.mkdir()
    sidecar = out / f"memory.db{suffix}"
    sidecar.write_bytes(_STALE)
    with pytest.raises(mv.RefusedError, match=suffix) as info:
        mv.restore(tree, out / "memory.db", keys=keys, vault_dir="sc", embed=False)
    assert info.value.exit_code == 2
    assert [p.name for p in out.iterdir()] == [sidecar.name]
    assert sidecar.read_bytes() == _STALE


def test_restore_refuses_a_sidecar_that_appears_while_writing(tmp_path, keys, monkeypatch):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    out = tmp_path / "out"
    out.mkdir()
    real_upsert_many = mdb.upsert_many

    def upsert_then_stale_wal(rows, conn, embed=True):
        (out / "memory.db-wal").write_bytes(_STALE)
        return real_upsert_many(rows, conn, embed=embed)

    monkeypatch.setattr(mdb, "upsert_many", upsert_then_stale_wal)
    with pytest.raises(mv.RefusedError, match="-wal"):
        mv.restore(tree, out / "memory.db", keys=keys, vault_dir="sc", embed=False)
    assert [p.name for p in out.iterdir()] == ["memory.db-wal"]


def _flip_last_byte(blob):
    return blob[:-1] + bytes([blob[-1] ^ 0x01])


def _row_rels(tree):
    return sorted(rel for rel in tree if rel != "vault.json")


def test_restore_stops_at_the_first_auth_failure_leaving_no_file(tmp_path, key_file, keys):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    rels = _row_rels(tree)
    for rel in rels[1:]:
        tree[rel] = _flip_last_byte(tree[rel])
    tree = _resigned(tree, keys)  # il mac e' valido: l'errore sta nel blob, non nell'albero
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(mv.BlobAuthError) as info:
        mv.restore(tree, out / "memory.db", keys=keys, vault_dir="sc", embed=False)
    first_bad = rels[1][len("rows/"):-len(".bin")]
    assert first_bad in str(info.value)
    assert info.value.exit_code == 3
    assert list(out.iterdir()) == []
    _assert_no_leak(_exception_text(info.value), _secrets_of(key_file, keys))


def test_restore_refuses_a_wrong_blob_key_id(tmp_path, key_file, keys):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    rel = _row_rels(tree)[0]
    other_id = bytes(b ^ 0xFF for b in keys.key_id)
    tree[rel] = tree[rel][:4] + other_id + tree[rel][8:]
    tree = _resigned(tree, keys)
    with pytest.raises(mv.BlobAuthError, match="key_id"):
        mv.restore(tree, tmp_path / "x.db", keys=keys, vault_dir="sc", embed=False)
    assert not (tmp_path / "x.db").exists()
    _assert_no_leak(_exception_text(_raised(mv.restore, tree, tmp_path / "x.db", keys)),
                    _secrets_of(key_file, keys))


def _raised(func, tree, target, keys, vault_dir="sc"):
    try:
        func(tree, target, keys=keys, vault_dir=vault_dir, embed=False)
    except mv.VaultError as exc:
        return exc
    raise AssertionError("expected a VaultError")


def test_restore_refuses_a_vault_written_with_another_key(tmp_path, keys):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    other = mv.create_key_file(tmp_path / "other.key")
    with pytest.raises(mv.RefusedError, match="key_id"):
        mv.restore(tree, tmp_path / "x.db", keys=other, vault_dir="sc", embed=False)
    assert not (tmp_path / "x.db").exists()


def test_restore_refuses_swapped_blobs_and_other_dir(tmp_path, keys):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    # l'albero sc presentato come ds: il mac lega la cartella, quindi e' un rifiuto (exit 2)
    with pytest.raises(mv.RefusedError, match="mac"):
        mv.restore(tree, tmp_path / "ds.db", keys=keys, vault_dir="ds", embed=False)
    # con il mac rifatto per ds (chi ha la chiave) resta l'AD dei blob a fermarlo
    with pytest.raises(mv.BlobAuthError):
        mv.restore(_resigned(tree, keys, vault_dir="ds"), tmp_path / "ds.db", keys=keys,
                   vault_dir="ds", embed=False)
    first, second = _row_rels(tree)[:2]
    swapped = dict(tree, **{first: tree[second], second: tree[first]})
    # senza chiave lo scambio rompe il mac; con il mac rifatto lo ferma l'AD del blob
    with pytest.raises(mv.RefusedError, match="mac"):
        mv.restore(swapped, tmp_path / "sw0.db", keys=keys, vault_dir="sc", embed=False)
    with pytest.raises(mv.BlobAuthError):
        mv.restore(_resigned(swapped, keys), tmp_path / "sw.db", keys=keys, vault_dir="sc",
                   embed=False)
    with pytest.raises(mv.VaultFormatError):
        mv.restore(tree, tmp_path / "xx.db", keys=keys, vault_dir="xx", embed=False)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keydir", "src.db"]


def test_restore_refuses_inconsistent_trees(tmp_path, key_file, keys):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    stray = dict(tree, **{"notes.txt": b"x"})
    no_meta = {rel: blob for rel, blob in tree.items() if rel != "vault.json"}
    # vault.json firmato con una riga in meno di quelle che l'albero porta (writer difettoso)
    miscounted = _hand_signed(tree, _master_of(key_file), rows=2)
    assert mv.mac_matches(mv.read_vault_json(miscounted["vault.json"]), miscounted, keys, "sc")
    for bad in (stray, no_meta, miscounted):
        with pytest.raises(mv.VaultFormatError) as info:
            mv.restore(bad, tmp_path / "x.db", keys=keys, vault_dir="sc", embed=False)
        assert info.value.exit_code == 3
    assert not (tmp_path / "x.db").exists()


def _plant_record(tree, keys, record, vault_dir="sc"):
    """Albero con un blob autentico (chiave giusta, AD giusta) che porta `record` com'e'.

    Anche il mac e' valido: lo scrive chi ha la chiave, e il test arriva al controllo del record.
    """
    row_id = keys.row_id(record["path"])
    planted = dict(tree)
    planted[f"rows/{row_id}.bin"] = mv.seal(mv.canonical_json(record), keys, vault_dir, row_id)
    return _resigned(planted, keys)


@pytest.mark.parametrize("field, value", [
    ("tier", "shared"), ("tier", None), ("realm", "Personal"), ("realm", "untagged"),
    ("updated", "2026-03-01T00:00:00Z"), ("created", "2026-03-01T00:00:00"),
    ("created", None), ("text", None), ("name", 5), ("agent", ["meta"]),
    ("description", {"a": 1}), ("type", True),
])
def test_restore_refuses_authentic_records_that_cannot_round_trip(tmp_path, keys, field, value):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    planted = _plant_record(tree, keys, dict(_rows()[1], **{field: value}))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(mv.VaultFormatError, match="cannot round-trip") as info:
        mv.restore(planted, out / "memory.db", keys=keys, vault_dir="sc", embed=False)
    assert keys.row_id(_BETA) in str(info.value)
    assert info.value.exit_code == 3
    assert list(out.iterdir()) == []


def test_restore_failure_while_writing_removes_the_partial(tmp_path, keys, monkeypatch):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)

    def broken(rows, conn, embed=True):
        raise RuntimeError("simulated failure inside the transaction")

    monkeypatch.setattr(mdb, "upsert_many", broken)
    with pytest.raises(RuntimeError, match="simulated"):
        mv.restore(tree, tmp_path / "x.db", keys=keys, vault_dir="sc", embed=False)
    assert not (tmp_path / "x.db").exists()
    assert not (tmp_path / "x.db.partial").exists()


# --- Errori senza byte di chiave su stderr -----------------------------------

_DRIVER = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import memvault as mv
mode, key = sys.argv[2], sys.argv[3]
keys = mv.load_keys(key)
if mode == "restore":
    root = Path(sys.argv[4])
    tree = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    mv.restore(tree, sys.argv[5], keys=keys, vault_dir="sc", embed=False)
"""


def _run_driver(*args):
    return subprocess.run(
        [sys.executable, "-c", _DRIVER, _HERE, *map(str, args)],
        capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
    )


def _write_tree(tree, root):
    for rel, blob in tree.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    return root


@pytest.mark.parametrize("case", ["missing header", "short key", "long key"])
def test_bad_key_file_errors_carry_no_key_bytes_on_stderr(tmp_path, case):
    master = os.urandom(32)
    path = tmp_path / "bad.key"
    path.write_bytes(_bad_key_files(master)[case])
    result = _run_driver("load", path)
    assert result.returncode != 0
    assert b"KeyFileError" in result.stderr
    _assert_no_leak(result.stderr + result.stdout, [master])


@pytest.mark.parametrize("fault", ["tampered", "wrong key_id", "other key", "bad mac"])
def test_restore_errors_carry_no_key_bytes_on_stderr(tmp_path, key_file, keys, fault):
    tree = _export(_build_db(tmp_path / "src.db", _rows()), keys)
    rel = _row_rels(tree)[0]
    used_key = key_file
    if fault == "tampered":
        tree[rel] = _flip_last_byte(tree[rel])
        tree = _resigned(tree, keys)
    elif fault == "wrong key_id":
        tree[rel] = tree[rel][:4] + bytes(b ^ 0x5A for b in keys.key_id) + tree[rel][8:]
        tree = _resigned(tree, keys)
    elif fault == "bad mac":
        tree[rel] = _flip_last_byte(tree[rel])  # vault.json non rifatto: il mac non torna
    else:
        used_key = tmp_path / "other.key"
        mv.create_key_file(used_key)
    root = _write_tree(tree, tmp_path / "tree")
    result = _run_driver("restore", used_key, root, tmp_path / "out.db")
    assert result.returncode != 0
    assert b"Error" in result.stderr
    if fault == "bad mac":
        assert b"RefusedError" in result.stderr
    assert not (tmp_path / "out.db").exists()
    secrets = _secrets_of(key_file, keys) + _secrets_of(used_key, mv.load_keys(used_key))
    _assert_no_leak(result.stderr + result.stdout, secrets)


# --- Senza cryptography ------------------------------------------------------


def _block_cryptography(monkeypatch):
    for name in list(sys.modules):
        if name == "cryptography" or name.startswith("cryptography."):
            monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "cryptography", None)


def test_without_cryptography_key_and_id_status_still_work(tmp_path, key_file, monkeypatch):
    db = _build_db(tmp_path / "src.db", _rows())
    keys = mv.load_keys(key_file)
    full = _export(db, keys)
    tree = dict(full)
    del tree[_row_rels(tree)[0]]
    _block_cryptography(monkeypatch)
    keys = mv.load_keys(key_file)  # la chiave si carica con la sola libreria standard
    # il mac e' HMAC della libreria standard: lo stato lo verifica anche senza cryptography
    assert mv.id_status(db, full, keys=keys, vault_dir="sc")["mac_ok"] is True
    status = mv.id_status(db, tree, keys=keys, vault_dir="sc")
    assert status == {"key_match": True, "mac_ok": False, "remote_writer": _WRITER,
                      "local_rows": 3, "remote_rows": 2, "would_add": 1, "would_remove": 0}
    other = mv.VaultKeys(os.urandom(32))
    assert mv.id_status(db, tree, keys=other, vault_dir="sc")["key_match"] is False
    assert mv.id_status(db, {}, keys=keys, vault_dir="sc")["key_match"] is None
    with pytest.raises(mv.CryptoUnavailableError) as info:
        _export(db, keys)
    assert isinstance(info.value, ImportError)
    assert info.value.exit_code == 3
    assert "cryptography==50.0.2" in str(info.value)
    # albero coerente (rows e mac rifatti) senza ricifrare nulla: arriva fino alla decifratura
    with pytest.raises(mv.CryptoUnavailableError):
        mv.restore(_resigned(tree, keys), tmp_path / "x.db", keys=keys, vault_dir="sc")
    assert not (tmp_path / "x.db").exists()
    assert not (tmp_path / "x.db.partial").exists()


def test_importing_memvault_does_not_import_cryptography():
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import memvault; "
            "print('cryptography' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", code, _HERE], capture_output=True,
                            text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


# --- Confronto ---------------------------------------------------------------


def test_compare_separates_differences_newer_local_and_removed_after_push(tmp_path, keys):
    src = _build_db(tmp_path / "src.db", _rows())
    restored = tmp_path / "drill.db"
    mv.restore(_export(src, keys), restored, keys=keys, vault_dir="sc", embed=False)
    # dopo il push: una riga nuova, una aggiornata (updated avanza), una potata
    mdb.upsert(db_path=src, path="instance/meta/delta.md", tier="instance", agent="meta",
               type="feedback", name="delta", description="new", text="delta body",
               created=_T3, updated=_T3, realm=None)
    mdb.upsert(db_path=src, **dict(_rows()[0], text="alpha rewritten", updated=None))
    beta_id = _query(src, f"SELECT id FROM memories WHERE path = '{_BETA}'")[0][0]
    mdb._prune_row(beta_id, src)
    # una differenza vera: stesso updated, descrizione diversa nella copia del vault
    conn = mdb._connect(restored)
    try:
        conn.execute("UPDATE memories SET description = 'drifted' WHERE path = ?", (_GAMMA,))
        conn.commit()
    finally:
        conn.close()
    report = mv.compare(restored, src)
    assert report["differences"] == [{"path": _GAMMA, "fields": ["description"]}]
    assert report["newer_local"] == sorted([_ALPHA, "instance/meta/delta.md"])
    assert report["removed_after_push"] == [_BETA]
    assert report["compared"] == 3 and report["vault_rows"] == 3
    assert "alpha rewritten" not in repr(report) and "drifted" not in repr(report)


def test_compare_lists_a_row_with_a_naive_local_updated_instead_of_raising(tmp_path, keys):
    src = _build_db(tmp_path / "src.db", _rows())
    restored = tmp_path / "drill.db"
    mv.restore(_export(src, keys), restored, keys=keys, vault_dir="sc", embed=False)
    # un writer legacy o SQL diretto: updated senza offset e testo cambiato
    conn = mdb._connect(src)
    try:
        conn.execute("UPDATE memories SET updated = ?, text = ? WHERE path = ?",
                     ("2026-12-01T00:00:00", "Beta rewritten by a legacy writer.", _BETA))
        conn.commit()
    finally:
        conn.close()
    report = mv.compare(restored, src)
    assert report["differences"] == [{"path": _BETA, "fields": ["text", "updated"]}]
    assert report["newer_local"] == [] and report["removed_after_push"] == []


def test_exit_codes():
    assert mv.RefusedError.exit_code == 2
    for cls in (mv.VaultError, mv.KeyFileError, mv.BlobAuthError, mv.VaultFormatError,
                mv.CryptoUnavailableError):
        assert cls.exit_code == 3
