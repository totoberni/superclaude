"""Active superclaude profile and per-profile data homes (single source of truth).

Code and configuration are shared by every profile and always live in ``~/.claude``
(see SHARED_CODE_ENTRIES). Data (memory DB, plans, comms, state, tmp, logs, caches,
git-policy flag) lives in the data home of the active profile.

Stdlib only: the hook dispatcher runs ``python -S``.
"""
from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

ENV_VAR = "SC_PROFILE"
DEFAULT_PROFILE = "superclaude"

# Cartella della data home di ogni profilo, relativa alla HOME utente
DATA_HOME_DIRS: dict[str, str] = {"superclaude": ".claude", "deepseek": ".claude-deepseek"}

# File privati di un profilo che vivono FUORI dalla sua data home, relativi alla HOME utente (glob).
# ~/.claude.json di superclaude contiene l'account claude.ai, i permessi per progetto e i server MCP;
# il .claude.json di deepseek sta gia' dentro la sua data home (CLAUDE_CONFIG_DIR).
PROFILE_PRIVATE_FILES: dict[str, tuple[str, ...]] = {
    "superclaude": (".claude.json", ".claude.json.backup*"),
}

# Forme Win32 di un disco locale, con barre in entrambi i versi:
# \\?\C:  \\.\C:  \\?\UNC\localhost\C$  \\localhost\C$  \\127.0.0.1\C$
_LOCAL_DEVICE = re.compile(
    r"[\\/]{2}(?:[?.][\\/](?P<d1>[a-z]):"
    r"|(?:[?.][\\/]unc[\\/])?(?:localhost|127\.0\.0\.1)[\\/](?P<d2>[a-z])\$)",
    re.I,
)

# Voci di ~/.claude condivise tra i profili (codice e configurazione, non dati)
SHARED_CODE_ENTRIES: tuple[str, ...] = (
    "skills", "agents", "rules", "hooks", "bin", "scripts", "lib", "docs", "upstream",
    ".venv", ".cache", "CLAUDE.md", "CLAUDE.md.example", "README.md",
    "dependencies.yml", ".gitattributes", ".gitignore",
)

# Voci della data home di un profilo che superclaude puo' preparare (file di installazione).
# Tutto il resto di quella home e' dato privato del profilo; settings*.json restano in sola lettura.
PROFILE_SETUP_ENTRIES: dict[str, tuple[str, ...]] = {
    "deepseek": ("bin", ".venv", "CLAUDE.md", "skills", "agents", "rules"),
}


# File di impostazioni di Claude Code: vivono nella data home del profilo (= CLAUDE_CONFIG_DIR)
SETTINGS_FILES: tuple[str, ...] = ("settings.json", "settings.local.json")
# Flag /git per profilo, relativo alla data home
GIT_POLICY_REL = "config/git-policy"


class ProfileError(ValueError):
    """Raised for an unknown SC_PROFILE value (fail closed, never fall back)."""


def code_home() -> Path:
    return Path.home() / ".claude"


def profiles() -> dict[str, Path]:
    # Path.home() viene letto a ogni chiamata, cosi' i test possono ridefinire USERPROFILE/HOME
    home = Path.home()
    return {name: home / dir_name for name, dir_name in DATA_HOME_DIRS.items()}


def active_profile(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    raw = env.get(ENV_VAR, "").strip().lower()
    if not raw:
        return DEFAULT_PROFILE
    if raw not in profiles():
        raise ProfileError(f"unknown {ENV_VAR}={raw!r}; expected one of {sorted(profiles())}")
    return raw


def data_home(profile: str | None = None) -> Path:
    name = profile or active_profile()
    try:
        return profiles()[name]
    except KeyError:
        raise ProfileError(f"unknown profile {name!r}; expected one of {sorted(profiles())}") from None


def memory_db_path(profile: str | None = None) -> Path:
    return data_home(profile) / "agent-memory" / ".memory.db"


def settings_paths(profile: str | None = None) -> list[Path]:
    return [data_home(profile) / name for name in SETTINGS_FILES]


def git_policy_path(profile: str | None = None) -> Path:
    return data_home(profile) / GIT_POLICY_REL


def other_profiles(profile: str | None = None) -> list[str]:
    name = profile or active_profile()
    return [p for p in profiles() if p != name]


def _parts(path: Path) -> tuple[str, ...]:
    # Confronto per componenti e senza distinzione di maiuscole (NTFS): ~/.claude-deepseek
    # non deve risultare "dentro" ~/.claude come accadrebbe con un confronto tra stringhe
    return tuple(os.path.normcase(p) for p in Path(os.path.abspath(path)).parts)


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    p, r = _parts(Path(path)), _parts(Path(root))
    return len(p) >= len(r) and p[: len(r)] == r


def profile_of_path(path: str | os.PathLike[str]) -> str | None:
    """Profile whose data home contains ``path`` (already canonicalised by the caller), else None."""
    for name, home in profiles().items():
        if is_within(path, home):
            return name
    return None


def local_form(text: str) -> str:
    """Riscrive le forme device/UNC di un disco locale (anche dentro un testo) nella forma `C:`."""
    return _LOCAL_DEVICE.sub(lambda m: (m.group("d1") or m.group("d2")) + ":", text)


def resolved_forms(path: str | os.PathLike[str]) -> set[str]:
    # abspath per i percorsi relativi, realpath per giunzioni e symlink (NTFS)
    raw = os.path.abspath(os.path.expanduser(local_form(os.fspath(path))))
    return {raw, os.path.realpath(raw)}


def foreign_profile_of(path: str | os.PathLike[str], profile: str | None = None) -> str | None:
    """Name of the OTHER profile whose data home contains ``path`` (junction-aware), else None."""
    active = profile or active_profile()
    forms = resolved_forms(path)
    homes = resolved_forms(Path.home())
    for name in other_profiles(active):
        # Anche la data home puo' essere una giunzione: si confrontano entrambe le forme
        for root in resolved_forms(data_home(name)):
            if any(is_within(form, root) for form in forms):
                return name
        if any(private_file_owner(form, homes) == name for form in forms):
            return name
    return None


def private_owner_of_name(name: str) -> str | None:
    """Profilo che possiede il file privato chiamato ``name`` nella HOME utente, else None."""
    low = os.path.normcase(name)
    for profile, patterns in PROFILE_PRIVATE_FILES.items():
        if any(fnmatch.fnmatchcase(low, os.path.normcase(pat)) for pat in patterns):
            return profile
    return None


def private_file_owner(path: str | os.PathLike[str], homes: Iterable[str] | None = None) -> str | None:
    """Profilo del file privato ``path`` (figlio diretto della HOME; ``homes`` = forme della HOME), else None."""
    p = _parts(Path(local_form(os.fspath(path))))
    for root in (resolved_forms(Path.home()) if homes is None else homes):
        r = _parts(Path(root))
        if len(p) == len(r) + 1 and p[:-1] == r:
            return private_owner_of_name(p[-1])
    return None
