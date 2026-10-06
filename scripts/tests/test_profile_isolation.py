"""Isolamento tra profili di script e shim: percorsi migrati, muro sul DB, fail closed.

Ogni test usa una HOME finta (USERPROFILE + HOME) sotto tmp_path: mai le home reali.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
MEMORY = SCRIPTS / "memory"
SHIM = ROOT / "bin" / "sc_shim.py"
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(MEMORY))
import db_wall  # noqa: E402
import memory_db  # noqa: E402
import sc_profile  # noqa: E402

# Percorsi DATI: relativi alla data home, identici a oggi sotto ~/.claude
DATA_KEYS = (
    "memory_db.DB_PATH", "scan.db_path", "mem_health._db_path", "render.CACHE_DIR",
    "viewer.GATE_PATH", "status.NB_PROGRESS_PATH", "status.COST_CACHE_PATH",
    "status.RATE_LATEST_PATH", "status.COST_REFRESH_STAMP", "status.CONTEXT_LATEST_PATH",
    "cost.PROJECTS_DIR", "cost.CACHE_PATH", "cost.LEDGER_PATH", "cost.RATE_LATEST_PATH",
    "nbmon.DEFAULT_PROGRESS_PATH", "spawn.LOG", "health.default_data_root",
)
# Percorsi CODICE: restano in ~/.claude per ogni profilo (modello di embedding compreso)
CODE_KEYS = ("memory_db.EMBED_CACHE_DIR", "shim.CLAUDE_HOME", "health.root_default")

# Il probe stampa {chiave: [percorso nuovo, formula di oggi]}; la formula di oggi e' quella
# hardcoded prima della migrazione, valutata con la stessa HOME.
_PROBE = r'''
import importlib.util, json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts" / "memory"))

def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, root / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass risolve il modulo da sys.modules
    spec.loader.exec_module(mod)
    return mod

H = os.path.expanduser("~")
old = lambda *p: os.path.join(H, ".claude", *p)
mdb = load("memory_db", "scripts/memory/memory_db.py")
scan = load("scan_mem_matrix", "scripts/memory/scan_mem_matrix.py")
mh = load("mem_health", "scripts/memory/mem_health.py")
render = load("render", "scripts/memory/render.py")
viewer = load("viewer", "scripts/memory/viewer.py")
st = load("statusline_telemetry", "scripts/statusline_telemetry.py")
cost = load("cost_cache_computer", "scripts/cost_cache_computer.py")
nbm = load("nb_monitor", "scripts/nb-monitor.py")
spawn = load("spawn_log_summary", "scripts/spawn_log_summary.py")
sh = load("super_health", "scripts/health/super_health.py")
shim = load("sc_shim", "bin/sc_shim.py")
out = {
    "memory_db.DB_PATH": [mdb.DB_PATH, old("agent-memory", ".memory.db")],
    "scan.db_path": [scan.db_path(), old("agent-memory", ".memory.db")],
    "mem_health._db_path": [mh._db_path(), old("agent-memory", ".memory.db")],
    "render.CACHE_DIR": [render.CACHE_DIR, Path(os.path.expanduser("~/.claude/agent-memory/.render-cache"))],
    "viewer.GATE_PATH": [viewer.GATE_PATH, old("agent-memory", ".viewer", "gate.html")],
    "status.NB_PROGRESS_PATH": [st.NB_PROGRESS_PATH, old(".nb-progress.json")],
    "status.COST_CACHE_PATH": [st.COST_CACHE_PATH, old(".cost-cache.json")],
    "status.RATE_LATEST_PATH": [st.RATE_LATEST_PATH, old(".rate-latest.json")],
    "status.COST_REFRESH_STAMP": [st.COST_REFRESH_STAMP, old(".cost-refresh.stamp")],
    "status.CONTEXT_LATEST_PATH": [st.CONTEXT_LATEST_PATH, old(".context-latest.json")],
    "cost.PROJECTS_DIR": [cost.PROJECTS_DIR, old("projects")],
    "cost.CACHE_PATH": [cost.CACHE_PATH, old(".cost-cache.json")],
    "cost.LEDGER_PATH": [cost.LEDGER_PATH, old(".cost-ledger.json")],
    "cost.RATE_LATEST_PATH": [cost.RATE_LATEST_PATH, old(".rate-latest.json")],
    "nbmon.DEFAULT_PROGRESS_PATH": [nbm.DEFAULT_PROGRESS_PATH, old(".nb-progress.json")],
    "spawn.LOG": [spawn.LOG, old("comms", "_spawns.log")],
    "health.default_data_root": [sh.default_data_root(Path.home() / ".claude"), old()],
    "memory_db.EMBED_CACHE_DIR": [mdb.EMBED_CACHE_DIR, old(".cache", "fastembed")],
    "shim.CLAUDE_HOME": [shim.CLAUDE_HOME, old()],
    "health.root_default": [sh.parse_args([]).root, old()],
}
print(json.dumps({k: [str(a), str(b)] for k, (a, b) in out.items()}))
'''

_ENV_DROP = ("SC_PROFILE", "MEMORY_DB_PATH", "CLAUDE_DIR")


def _env(home: Path, profile: str | None = None, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_DROP}
    env.update(USERPROFILE=str(home), HOME=str(home), PYTHONUTF8="1", HF_HUB_OFFLINE="1",
               PYTHONDONTWRITEBYTECODE="1", **extra)
    if profile is not None:
        env["SC_PROFILE"] = profile
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=300)


def _probe(home: Path, profile: str | None) -> dict[str, list[str]]:
    proc = _run(["-c", _PROBE, str(ROOT)], _env(home, profile))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _make_dir_link(link: Path, target: Path) -> None:
    # Le giunzioni NTFS non richiedono privilegi admin, a differenza dei symlink
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.delenv("SC_PROFILE", raising=False)
    return fake


def test_unset_profile_paths_are_byte_identical_to_today(home: Path) -> None:
    paths = _probe(home, None)
    assert set(paths) == set(DATA_KEYS) | set(CODE_KEYS)
    for key, (new, today) in paths.items():
        assert new == today, key
        assert new.startswith(str(home / ".claude")), key


def test_deepseek_moves_data_and_keeps_code_shared(home: Path) -> None:
    paths = _probe(home, "deepseek")
    claude, deepseek = str(home / ".claude"), str(home / ".claude-deepseek")
    for key in DATA_KEYS:
        new, today = paths[key]
        assert new == deepseek + today[len(claude):], key
    for key in CODE_KEYS:
        new, today = paths[key]
        assert new == today, key
    # Il modello di embedding resta nella cache condivisa del codice
    assert paths["memory_db.EMBED_CACHE_DIR"][0] == str(home / ".claude" / ".cache" / "fastembed")
    assert not (home / ".claude-deepseek").exists()


def test_invalid_profile_fails_closed_in_shim_and_modules(home: Path) -> None:
    proc = _run([str(SHIM), "mem", "stats"], _env(home, "bogus"))
    assert proc.returncode == 2
    assert "unknown SC_PROFILE='bogus'" in proc.stderr
    proc = _run([str(MEMORY / "memory_db.py"), "stats"], _env(home, "bogus"))
    assert proc.returncode != 0
    assert "ProfileError" in proc.stderr
    assert not (home / ".claude").exists() and not (home / ".claude-deepseek").exists()


@pytest.fixture
def shim_home(home: Path) -> Iterator[Path]:
    # Lo shim cerca gli script nella code home (~/.claude/scripts): nella HOME finta
    # una giunzione la fa puntare alla copia sotto test
    (home / ".claude").mkdir()
    link = home / ".claude" / "scripts"
    _make_dir_link(link, SCRIPTS)
    yield home
    # Si rimuove solo il collegamento, mai il contenuto degli script veri
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


def test_mem_init_under_deepseek_creates_own_db(shim_home: Path) -> None:
    home = shim_home
    proc = _run([str(SHIM), "mem", "init"], _env(home, "deepseek"))
    assert proc.returncode == 0, proc.stderr
    db = home / ".claude-deepseek" / "agent-memory" / ".memory.db"
    assert db.is_file()
    assert str(db) in proc.stdout
    assert not (home / ".claude" / "agent-memory").exists()


@pytest.mark.parametrize(("profile", "foreign_home"), [(None, ".claude-deepseek"), ("deepseek", ".claude")])
def test_wall_refuses_foreign_db_in_process(home: Path, monkeypatch: pytest.MonkeyPatch,
                                            profile: str | None, foreign_home: str) -> None:
    if profile:
        monkeypatch.setenv("SC_PROFILE", profile)
    foreign = home / foreign_home / "agent-memory" / ".memory.db"
    with pytest.raises(db_wall.CrossProfileDBError, match="cross-profile"):
        db_wall.assert_own_profile_path(foreign)
    with pytest.raises(db_wall.CrossProfileDBError):
        memory_db._connect(foreign)
    with pytest.raises(db_wall.CrossProfileDBError):
        memory_db.run_sql("SELECT 1", foreign)
    with pytest.raises(db_wall.CrossProfileDBError):
        memory_db.migrate(home / foreign_home / "agent-memory", db_path=home / "elsewhere.db")
    # Nessuna cartella creata nella home dell'altro profilo
    assert not (home / foreign_home).exists()
    own = sc_profile.memory_db_path()
    assert db_wall.assert_own_profile_path(own) == own
    assert db_wall.assert_own_profile_path(home / "scratch" / "t.db") == home / "scratch" / "t.db"


def test_wall_resolves_relative_paths(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(home)
    with pytest.raises(db_wall.CrossProfileDBError):
        db_wall.assert_own_profile_path(Path(".claude-deepseek") / "agent-memory" / ".memory.db")
    with pytest.raises(db_wall.CrossProfileDBError):
        db_wall.assert_own_profile_path(Path("code") / ".." / ".claude-deepseek" / "x.db")


def test_wall_follows_junction_into_foreign_home(home: Path, tmp_path: Path) -> None:
    target = home / ".claude-deepseek" / "agent-memory"
    target.mkdir(parents=True)
    link = tmp_path / "innocent"
    _make_dir_link(link, target)
    assert (link / ".memory.db").parent.exists()
    with pytest.raises(db_wall.CrossProfileDBError):
        db_wall.assert_own_profile_path(link / ".memory.db")
    with pytest.raises(db_wall.CrossProfileDBError):
        memory_db._connect(link / ".memory.db")
    assert not (target / ".memory.db").exists()


def test_wall_follows_junction_as_foreign_home(home: Path, tmp_path: Path) -> None:
    # La data home dell'altro profilo e' essa stessa una giunzione verso un'altra cartella
    real = tmp_path / "real-deepseek"
    (real / "agent-memory").mkdir(parents=True)
    _make_dir_link(home / ".claude-deepseek", real)
    with pytest.raises(db_wall.CrossProfileDBError):
        db_wall.assert_own_profile_path(real / "agent-memory" / ".memory.db")


@pytest.mark.parametrize(("profile", "foreign_home"), [(None, ".claude-deepseek"), ("deepseek", ".claude")])
def test_cli_tools_refuse_foreign_db(shim_home: Path, profile: str | None, foreign_home: str) -> None:
    home = shim_home
    foreign_dir = home / foreign_home / "agent-memory"
    foreign = foreign_dir / ".memory.db"
    cases = [
        ([str(SHIM), "mem", "--db", str(foreign), "stats"], {}),
        ([str(MEMORY / "memory_db.py"), "--db", str(foreign), "init"], {}),
        ([str(MEMORY / "scan_mem_matrix.py"), "--json"], {"MEMORY_DB_PATH": str(foreign)}),
        ([str(MEMORY / "mem_health.py")], {"CLAUDE_DIR": str(home / foreign_home)}),
        ([str(MEMORY / "viewer.py"), "--db", str(foreign), "--all"], {}),
    ]
    for args, extra in cases:
        proc = _run(args, _env(home, profile, **extra))
        assert proc.returncode == 3, (args, proc.returncode, proc.stderr)
        assert "cross-profile memory access refused" in proc.stderr, args
    assert not foreign_dir.exists()


def test_cli_allows_db_outside_every_profile_home(shim_home: Path, tmp_path: Path) -> None:
    home = shim_home
    db = tmp_path / "outside" / ".memory.db"
    proc = _run([str(SHIM), "mem", "--db", str(db), "init"], _env(home, "deepseek"))
    assert proc.returncode == 0, proc.stderr
    assert db.is_file()


def _load_super_health():
    import importlib.util
    spec = importlib.util.spec_from_file_location("super_health_iso", SCRIPTS / "health" / "super_health.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass risolve il modulo da sys.modules
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(("profile", "expected"), [(None, ".claude"), ("deepseek", ".claude-deepseek")])
def test_super_health_reads_settings_from_profile_data_home(home: Path, monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path, profile: str | None,
                                                            expected: str) -> None:
    sh = _load_super_health()
    for name in (".claude", ".claude-deepseek"):
        (home / name).mkdir()
        (home / name / "settings.json").write_text(json.dumps({"home": name}), encoding="utf-8")
    if profile:
        monkeypatch.setenv("SC_PROFILE", profile)
    ctx = sh.build_context(home / ".claude")
    assert ctx.root == home / ".claude"
    assert ctx.data == home / expected
    assert ctx.settings == {"home": expected}
    # Una radice sintetica (come nei test di super_health) resta anche radice dati
    synthetic = tmp_path / "synthetic"
    synthetic.mkdir()
    assert sh.build_context(synthetic).data == synthetic


# --------------------------------------------------------------------------
# upsert --text-file: stesso muro di --db
# --------------------------------------------------------------------------
# Cache del modello di embedding vera, catturata prima che i test ridefiniscano la HOME
_REAL_EMBED_CACHE = Path.home() / ".claude" / ".cache"


def _upsert_args(db: Path, body: Path) -> list[str]:
    return [str(MEMORY / "memory_db.py"), "--db", str(db), "upsert", "--tier", "instance",
            "--type", "user", "--name", "wall_probe", "--description", "d", "--text-file", str(body)]


@pytest.mark.parametrize(("profile", "foreign_home"), [(None, ".claude-deepseek"), ("deepseek", ".claude")])
def test_text_file_refuses_foreign_profile_home(home: Path, tmp_path: Path,
                                                profile: str | None, foreign_home: str) -> None:
    secret = home / foreign_home / "plans" / "secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("foreign profile data", encoding="utf-8")
    db = tmp_path / "outside" / "t.db"
    # Anche un file inesistente: il muro precede il controllo di esistenza
    for body in (secret, secret.with_name("missing.md")):
        proc = _run(_upsert_args(db, body), _env(home, profile))
        assert proc.returncode == 3, (body, proc.returncode, proc.stderr)
        assert "--text-file refused" in proc.stderr
        assert "cross-profile memory access refused" in proc.stderr
    assert not db.exists()


def test_text_file_refuses_junction_into_foreign_home(home: Path, tmp_path: Path) -> None:
    target = home / ".claude-deepseek" / "plans"
    target.mkdir(parents=True)
    (target / "secret.md").write_text("foreign profile data", encoding="utf-8")
    link = tmp_path / "innocent"
    _make_dir_link(link, target)
    db = tmp_path / "outside" / "t.db"
    proc = _run(_upsert_args(db, link / "secret.md"), _env(home))
    assert proc.returncode == 3, proc.stderr
    assert "cross-profile memory access refused" in proc.stderr
    assert not db.exists()


@pytest.fixture
def embed_home(home: Path) -> Iterator[Path]:
    # Il modello di embedding sta nella code home: la HOME finta lo raggiunge con una
    # giunzione verso la cache vera (sola lettura del modello, nessun dato di memoria)
    (home / ".claude").mkdir()
    link = home / ".claude" / ".cache"
    _make_dir_link(link, _REAL_EMBED_CACHE)
    yield home
    # Si rimuove solo il collegamento, mai la cache vera
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


@pytest.mark.parametrize(("profile", "where"), [(None, ".claude"), ("deepseek", ".claude-deepseek"),
                                               ("deepseek", None)])
def test_text_file_allows_own_home_and_outside(embed_home: Path, tmp_path: Path,
                                              profile: str | None, where: str | None) -> None:
    home = embed_home
    body = (home / where / "plans" if where else tmp_path / "outside") / "body.md"
    body.parent.mkdir(parents=True)
    body.write_text("allowed body text", encoding="utf-8")
    db = tmp_path / "scratch" / "t.db"
    db.parent.mkdir()
    memory_db.init_db(db)
    proc = _run(_upsert_args(db, body), _env(home, profile))
    assert proc.returncode == 0, proc.stderr
    _cols, rows = memory_db.run_sql("SELECT text FROM memories WHERE name = 'wall_probe'", db)
    assert rows == [("allowed body text",)]


# --------------------------------------------------------------------------
# statusline: SC_PROFILE non valido = una riga, nessun traceback
# --------------------------------------------------------------------------
def test_statusline_invalid_profile_prints_one_line(home: Path) -> None:
    proc = subprocess.run([sys.executable, str(SCRIPTS / "statusline_telemetry.py")], input="{}",
                          capture_output=True, text=True, encoding="utf-8",
                          env=_env(home, "bogus"), timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "SC_PROFILE invalid: bogus"
    assert proc.stderr == ""
    assert not (home / ".claude").exists() and not (home / ".claude-deepseek").exists()
