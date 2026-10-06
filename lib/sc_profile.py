"""Active superclaude profile and per-profile data homes (single source of truth).

Code and configuration are shared by every profile and always live in ``~/.claude``
(see SHARED_CODE_ENTRIES). Data (memory DB, plans, comms, state, tmp, logs, caches,
git-policy flag) lives in the data home of the active profile.

Stdlib only: the hook dispatcher runs ``python -S``.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ENV_VAR = "SC_PROFILE"
DEFAULT_PROFILE = "superclaude"

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
    return {"superclaude": home / ".claude", "deepseek": home / ".claude-deepseek"}


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


def resolved_forms(path: str | os.PathLike[str]) -> set[str]:
    # abspath per i percorsi relativi, realpath per giunzioni e symlink (NTFS)
    raw = os.path.abspath(os.path.expanduser(os.fspath(path)))
    return {raw, os.path.realpath(raw)}


def foreign_profile_of(path: str | os.PathLike[str], profile: str | None = None) -> str | None:
    """Name of the OTHER profile whose data home contains ``path`` (junction-aware), else None."""
    active = profile or active_profile()
    forms = resolved_forms(path)
    for name in other_profiles(active):
        # Anche la data home puo' essere una giunzione: si confrontano entrambe le forme
        for root in resolved_forms(data_home(name)):
            if any(is_within(form, root) for form in forms):
                return name
    return None
