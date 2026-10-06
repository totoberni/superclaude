"""Muro in-process tra profili: nessun profilo apre il DB di memoria (o i suoi dati) di un altro.

Unico punto di controllo per memory_db, scan_mem_matrix, mem_health e viewer.
I percorsi fuori da ogni data home (es. i DB temporanei dei test) restano ammessi.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# sc_profile vive in <code home>/lib: il percorso si ricava da questo file, cosi'
# funziona sia dall'albero live sia da una copia di staging
_LIB_DIR = str(Path(__file__).resolve().parents[2] / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import sc_profile  # noqa: E402


class CrossProfileDBError(PermissionError):
    """Raised when a memory path resolves inside another profile's data home."""


# La regola vive in sc_profile (unica fonte di verita', usata anche dal firewall degli hook)
foreign_profile_of = sc_profile.foreign_profile_of


def assert_own_profile_path(path: str | os.PathLike[str], profile: str | None = None) -> Path:
    """Return ``path`` unchanged, or raise CrossProfileDBError if it belongs to another profile."""
    owner = foreign_profile_of(path, profile)
    if owner is not None:
        active = profile or sc_profile.active_profile()
        raise CrossProfileDBError(
            f"cross-profile memory access refused: {path} is inside the data home of profile "
            f"{owner!r}; active profile {active!r} uses {sc_profile.memory_db_path(active)}"
        )
    return Path(path)
