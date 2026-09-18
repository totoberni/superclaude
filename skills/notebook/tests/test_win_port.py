"""Test del port Windows dello skill /notebook.

Coprono i punti in cui il codice dipendeva da POSIX: lock, I/O UTF-8 con LF,
risoluzione dell'interprete, avvisi OneDrive/UNC, chiave di sessione del kernel.
Girano anche su POSIX (i rami specifici di Windows sono marcati con skipif).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR))

import nb_exec  # noqa: E402
import nb_io  # noqa: E402
import nb_main  # noqa: E402

IS_WINDOWS = os.name == "nt"
ACCENTED = "Città: àèìòù, perché più giù"


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def test_lock_second_acquirer_fails_fast(tmp_path):
    lp = tmp_path / "nb.ipynb.lock"
    with nb_io.file_lock(lp, timeout=1.0):
        t0 = time.monotonic()
        with pytest.raises(TimeoutError) as exc:
            with nb_io.file_lock(lp, timeout=0.3):
                pass
        assert time.monotonic() - t0 < 3.0
        # Il messaggio deve nominare il detentore (pid corrente).
        assert str(os.getpid()) in str(exc.value)
    # Rilasciato: si riacquisisce subito.
    with nb_io.file_lock(lp, timeout=0.3):
        pass


def test_lock_holder_readable_while_locked(tmp_path):
    lp = tmp_path / "nb.ipynb.lock"
    with nb_io.file_lock(lp, timeout=1.0):
        holder = nb_io._read_lock_holder(lp)
    assert holder is not None and holder.split()[0] == str(os.getpid())


_HOLDER_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, {skill!r})
    import nb_io
    with nb_io.file_lock({lock!r}, timeout=5.0):
        print("LOCKED", os.getpid(), flush=True)
        time.sleep(120)
    """
)


def test_lock_released_when_holder_process_dies(tmp_path):
    lp = tmp_path / "nb.ipynb.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT.format(skill=str(SKILL_DIR), lock=str(lp))],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        tag, holder_pid = proc.stdout.readline().split()
        assert tag == "LOCKED"
        with pytest.raises(TimeoutError) as exc:
            with nb_io.file_lock(lp, timeout=0.3):
                pass
        assert holder_pid in str(exc.value)
    finally:
        # Morte brusca del detentore: nessuna possibilita' di rilascio esplicito.
        # Su Windows il python.exe di un venv e' un launcher e il vero interprete
        # e' un processo figlio: si uccide il pid dichiarato dal detentore.
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", holder_pid],
                           capture_output=True)
        proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()
    # Il sistema operativo rilascia il lock alla chiusura dell'handle.
    with nb_io.file_lock(lp, timeout=5.0):
        pass


def test_lock_path_is_case_and_separator_insensitive(tmp_path):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git non disponibile")
    repo = tmp_path / "Repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    nb = repo / "Demo.ipynb"
    nb.write_text("{}", encoding="utf-8")
    expected = nb_io.lock_path_for(nb)
    assert expected.parent == repo.resolve()
    if IS_WINDOWS:
        variant = Path(str(nb).swapcase().replace("\\", "/"))
        assert nb_io.same_path(nb_io.lock_path_for(variant), expected)


# ---------------------------------------------------------------------------
# UTF-8 / LF
# ---------------------------------------------------------------------------


def _notebook() -> dict:
    return {
        "cells": [
            {"cell_type": "markdown", "id": "a1", "metadata": {},
             "source": [f"# {ACCENTED}\n", "seconda riga"]},
            {"cell_type": "code", "id": "b2", "metadata": {}, "execution_count": None,
             "outputs": [], "source": [f"s = '{ACCENTED}'\n", "print(s)"]},
        ],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }


def test_atomic_write_roundtrip_utf8_lf(tmp_path):
    path = tmp_path / "accenti.ipynb"
    nb = _notebook()
    nb_io.atomic_write_ipynb(path, nb)
    raw = path.read_bytes()
    assert b"\r" not in raw
    assert "àèìòù".encode("utf-8") in raw  # niente escape \uXXXX, niente cp1252
    assert raw.endswith(b"\n")
    assert nb_io.load_ipynb(path) == nb
    # Seconda scrittura sopra un file esistente (os.replace su Windows).
    nb["cells"][0]["source"][1] = "riga cambiata: è"
    nb_io.atomic_write_ipynb(path, nb)
    assert nb_io.load_ipynb(path) == nb
    assert [p.name for p in tmp_path.iterdir()] == ["accenti.ipynb"]  # nessun .tmp residuo


def test_normalize_lf_preserves_mtime_and_text(tmp_path):
    py = tmp_path / "paired.py"
    py.write_bytes(f"# %%\r\ns = '{ACCENTED}'\r\n".encode("utf-8"))
    old = time.time() - 3600
    os.utime(py, (old, old))
    before = py.stat().st_mtime_ns
    assert nb_io.normalize_lf(py) is True
    assert py.read_bytes() == f"# %%\ns = '{ACCENTED}'\n".encode("utf-8")
    assert py.stat().st_mtime_ns == before
    assert nb_io.normalize_lf(py) is False  # idempotente


def test_init_append_helper_keeps_eol_style_and_utf8(tmp_path):
    import nb_init
    crlf = tmp_path / ".gitignore"
    crlf.write_bytes("# già presente: è\r\n".encode("utf-8"))
    nb_init._append_to_file_if_missing(crlf, "*.ipynb.lock", "*.ipynb.lock\n")
    assert crlf.read_bytes() == "# già presente: è\r\n*.ipynb.lock\r\n".encode("utf-8")
    fresh = tmp_path / "hook"
    nb_init._append_to_file_if_missing(fresh, "marker", "#!/bin/sh\nmarker\n")
    assert fresh.read_bytes() == b"#!/bin/sh\nmarker\n"


@pytest.mark.skipif(not IS_WINDOWS, reason="semantica di condivisione dei file di Windows")
def test_replace_with_retry_reports_open_target(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("nuovo", encoding="utf-8")
    dst.write_text("vecchio", encoding="utf-8")
    with open(dst, "rb"):  # open() di Python non concede FILE_SHARE_DELETE
        with pytest.raises(PermissionError) as exc:
            nb_io.replace_with_retry(str(src), str(dst), attempts=2, delay=0.05)
    assert "holds it open" in str(exc.value)
    nb_io.replace_with_retry(str(src), str(dst))
    assert dst.read_text(encoding="utf-8") == "nuovo"


# ---------------------------------------------------------------------------
# Risoluzione dell'interprete
# ---------------------------------------------------------------------------


def _fake_venv(root: Path) -> Path:
    py = (root / ".venv" / "Scripts" / "python.exe") if IS_WINDOWS \
        else (root / ".venv" / "bin" / "python")
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    return py


def test_venv_python_layout(tmp_path):
    assert nb_io.venv_python(tmp_path) is None
    py = _fake_venv(tmp_path)
    assert nb_io.venv_python(tmp_path) == py
    assert py.parent.name == ("Scripts" if IS_WINDOWS else "bin")


def test_find_project_python_walks_up(tmp_path):
    py = _fake_venv(tmp_path)
    nested = tmp_path / "notebooks" / "sub"
    nested.mkdir(parents=True)
    assert nb_io.find_project_python(nested / "x.ipynb") == py
    assert nb_io.find_project_python(nested) == py


def test_resolve_interpreter_priority(tmp_path):
    proj, other, cwd = tmp_path / "proj", tmp_path / "other", tmp_path / "cwd"
    for d in (proj, other, cwd):
        d.mkdir()
    proj_py, other_py, cwd_py = _fake_venv(proj), _fake_venv(other), _fake_venv(cwd)
    env = {"VIRTUAL_ENV": str(other / ".venv")}
    argv = ["execute", str(proj / "demo.ipynb"), "--cells", "tag:x"]
    # 1. il venv del progetto del notebook batte VIRTUAL_ENV e la cwd
    assert nb_main.resolve_interpreter(argv, cwd=cwd, environ=env) == proj_py
    # 2. senza notebook risolvibile: VIRTUAL_ENV
    argv2 = ["execute", str(tmp_path / "nowhere" / "demo.ipynb")]
    assert nb_main.resolve_interpreter(argv2, cwd=cwd, environ=env) == other_py
    # 3. altrimenti si risale dalla cwd
    assert nb_main.resolve_interpreter(argv2, cwd=cwd, environ={}) == cwd_py


def test_expand_home_only_touches_path_like_args():
    home = str(Path.home())
    out = nb_main._expand_home(["batch", "~/code/p/nb.ipynb", "--plan=~/.claude/plans/x.yml",
                                "a~b", "~user"])
    assert out == ["batch", home + "/code/p/nb.ipynb", f"--plan={home}/.claude/plans/x.yml",
                   "a~b", "~user"]


def test_fwd_and_same_path():
    assert nb_io.fwd(r"C:\Users\x\.venv\Scripts\python.exe") == "C:/Users/x/.venv/Scripts/python.exe"
    if IS_WINDOWS:
        assert nb_io.same_path(r"C:\Users\X\nb.ipynb", "c:/users/x/NB.ipynb")
    assert not nb_io.same_path("a/b.ipynb", "a/c.ipynb")


# ---------------------------------------------------------------------------
# OneDrive / UNC
# ---------------------------------------------------------------------------


@pytest.fixture
def no_onedrive(monkeypatch):
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


def test_onedrive_detection(monkeypatch, no_onedrive):
    assert nb_io.onedrive_root_for(r"C:\Users\x\OneDrive\proj\nb.ipynb") is None
    monkeypatch.setenv("OneDriveCommercial", r"C:\Users\x\OneDrive - ACME")
    # maiuscole e separatori diversi da quelli della variabile d'ambiente
    assert nb_io.onedrive_root_for("c:/users/X/onedrive - acme/proj/nb.ipynb")
    # un prefisso comune non basta: serve un confine di directory
    assert nb_io.onedrive_root_for(r"C:\Users\x\OneDrive - ACME2\nb.ipynb") is None
    assert nb_io.onedrive_root_for(r"C:\Users\x\code\proj\nb.ipynb") is None
    hazards = nb_io.path_hazards(r"C:\Users\x\OneDrive - ACME\proj\nb.ipynb")
    assert len(hazards) == 1 and "OneDrive" in hazards[0]


def test_onedrive_other_env_vars(monkeypatch, no_onedrive):
    monkeypatch.setenv("OneDriveConsumer", r"D:\Cloud\OneDrive")
    assert nb_io.onedrive_root_for(r"D:\Cloud\OneDrive\a\b.ipynb") == r"D:\Cloud\OneDrive"
    monkeypatch.delenv("OneDriveConsumer")
    monkeypatch.setenv("OneDrive", r"D:\Cloud\OneDrive")
    assert nb_io.onedrive_root_for(r"D:\Cloud\OneDrive\a\b.ipynb") == r"D:\Cloud\OneDrive"


def test_unc_detection(no_onedrive):
    assert nb_io.is_network_path(r"\\server\share\proj\nb.ipynb")
    assert nb_io.is_network_path("//server/share/proj/nb.ipynb")
    assert nb_io.is_network_path(r"\\?\UNC\server\share\nb.ipynb")
    assert not nb_io.is_network_path(r"\\?\C:\very\long\path\nb.ipynb")
    assert not nb_io.is_network_path("relative/nb.ipynb")
    msgs = nb_io.path_hazards(r"\\server\share\proj\nb.ipynb")
    assert len(msgs) == 1 and "network" in msgs[0]


def test_local_path_has_no_hazards(tmp_path, no_onedrive):
    assert nb_io.path_hazards(tmp_path / "nb.ipynb") == []


def test_init_warns_but_does_not_refuse(monkeypatch, no_onedrive, capsys, tmp_path):
    import nb_init
    monkeypatch.setenv("OneDrive", str(tmp_path))
    nb_init._warn_path_hazards(tmp_path / "proj" / "nb.ipynb")  # non deve uscire
    assert "WARNING" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Chiave di sessione del kernel
# ---------------------------------------------------------------------------


@pytest.fixture
def no_session_env(monkeypatch):
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)


def test_session_key_precedence(monkeypatch, no_session_env, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "code-123")
    assert nb_exec._session_id(tmp_path) == "code-123"
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
    assert nb_exec._session_id(tmp_path) == "sess-abc"


def test_session_key_is_filename_safe(monkeypatch, no_session_env, tmp_path):
    monkeypatch.setenv("CLAUDE_SESSION_ID", r"a/b\c:d*e" + "x" * 100)
    sid = nb_exec._session_id(tmp_path)
    assert len(sid) <= 40 and not set(sid) & set('/\\:*?"<>|')


@pytest.mark.skipif(not IS_WINDOWS, reason="su POSIX il fallback resta os.getsid(0)")
def test_session_key_fallback_is_per_project_and_stable(no_session_env, tmp_path):
    a, b = tmp_path / "projA", tmp_path / "projB"
    key_a = nb_exec._session_id(a)
    assert key_a.startswith("local-") and key_a == nb_exec._session_id(a)
    assert key_a != nb_exec._session_id(b)
    # Stessa radice scritta in modo diverso: stessa chiave.
    assert key_a == nb_exec._session_id(Path(str(a).upper().replace("\\", "/")))
    # Stabile tra processi diversi (niente getppid).
    out = subprocess.check_output(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(SKILL_DIR)!r}); import nb_exec; "
         f"print(nb_exec._session_id({str(a)!r}))"],
        text=True, env={k: v for k, v in os.environ.items()
                        if k not in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")},
    ).strip()
    assert out == key_a


def test_kernel_cache_paths_are_short(monkeypatch, no_session_env):
    cf = nb_exec._connection_file("p" * 200 + " con spazi/è", "s" * 36)
    assert len(cf.name) <= 40 + 1 + 36 + len(".json")
    assert " " not in cf.name and "/" not in cf.name
    meta = nb_exec._meta_file("proj", "sess")
    assert meta.parent == cf.parent and json.dumps({}) == "{}"


@pytest.mark.skipif(not IS_WINDOWS, reason="flag di creazione processo di Windows")
def test_windows_detach_flags():
    assert nb_exec._WIN_DETACH_FLAGS & subprocess.CREATE_NO_WINDOW
    assert nb_exec._WIN_DETACH_FLAGS & subprocess.CREATE_NEW_PROCESS_GROUP
