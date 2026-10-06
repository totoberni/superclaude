"""Punto d'ingresso unico degli hook: `python dispatch.py <Evento>`.

Un solo processo per evento: legge lo stdin una volta, carica gli handler,
aggrega i risultati. Deve restare veloce: solo libreria standard, interprete
di base (non il venv), import pigri dentro gli handler pesanti.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sc_hooks import core  # noqa: E402


def _load_handlers() -> None:
    import sc_hooks.handlers as pkg
    for mod in sorted(m.name for m in pkgutil.iter_modules(pkg.__path__)):
        try:
            importlib.import_module(f"sc_hooks.handlers.{mod}")
        except Exception as exc:  # noqa: BLE001 - un handler rotto non ferma gli altri
            core.log_error(f"import/{mod}", exc)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, ValueError):
            pass
    try:
        ctx = core.read_ctx(sys.argv[1] if len(sys.argv) > 1 else None)
        if not core.PROFILE_ERROR:
            # Con un profilo ignoto gli handler non si caricano: i loro percorsi dati non esistono
            _load_handlers()
        return core.dispatch(ctx)
    except Exception as exc:  # noqa: BLE001 - mai bloccare la sessione per un bug
        core.log_error("dispatch", exc)
        return 0


if __name__ == "__main__":
    sys.exit(main())
