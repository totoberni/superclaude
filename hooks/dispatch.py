"""Punto d'ingresso unico degli hook: `python dispatch.py <Evento>`.

Un solo processo per evento: legge lo stdin una volta, carica gli handler,
aggrega i risultati. Deve restare veloce: solo libreria standard, interprete
di base (non il venv), import pigri dentro gli handler pesanti.
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Copia locale di sc_profile.ENV_VAR / DEFAULT_PROFILE: serve proprio quando
# core o sc_profile non si importano, quindi non puo' dipendere da loro
_ENV_VAR = "SC_PROFILE"
_DEFAULT_PROFILE = "superclaude"
_FIREWALL_MODULE = "profile_firewall"

try:
    from sc_hooks import core  # noqa: E402
    _IMPORT_ERROR: BaseException | None = None
except Exception as _exc:  # noqa: BLE001 - un lib/ rotto non deve aprire il profilo isolato
    core = None  # type: ignore[assignment]
    _IMPORT_ERROR = _exc


def _env_non_default() -> bool:
    return os.environ.get(_ENV_VAR, "").strip().lower() not in ("", _DEFAULT_PROFILE)


def _import_failure(event: str) -> int:
    """core/sc_profile non importabili: fail closed sotto un profilo non predefinito."""
    if event == "PreToolUse" and _env_non_default():
        sys.stderr.write(
            f"GUARD-BLOCK [profile] fail closed: the hook core failed to import "
            f"({type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}) while {_ENV_VAR}="
            f"{os.environ.get(_ENV_VAR, '')!r}; every tool call is denied until the hooks are repaired\n"
        )
        return 2
    # Profilo predefinito (o evento senza blocco): comportamento di sempre, errore non bloccante
    raise _IMPORT_ERROR  # type: ignore[misc]


def _load_handlers() -> set[str]:
    """Importa gli handler; restituisce i moduli che non si sono importati."""
    import sc_hooks.handlers as pkg
    failed: set[str] = set()
    for mod in sorted(m.name for m in pkgutil.iter_modules(pkg.__path__)):
        try:
            importlib.import_module(f"sc_hooks.handlers.{mod}")
        except Exception as exc:  # noqa: BLE001 - un handler rotto non ferma gli altri
            core.log_error(f"import/{mod}", exc)
            failed.add(mod)
    return failed


def _firewall_missing(event: str) -> int | None:
    """Firewall non importato sotto un profilo isolato: ogni PreToolUse e' negato."""
    if event != "PreToolUse" or not core.non_default_profile():
        return None
    sys.stderr.write(
        f"GUARD-BLOCK [profile] fail closed: the profile firewall failed to import under the "
        f"{core.PROFILE} profile; every tool call is denied, see the hooks log\n"
    )
    return 2


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, ValueError):
            pass
    event_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if core is None:
        return _import_failure(event_arg or "")
    try:
        ctx = core.read_ctx(event_arg)
        if not core.PROFILE_ERROR:
            # Con un profilo ignoto gli handler non si caricano: i loro percorsi dati non esistono
            if _FIREWALL_MODULE in _load_handlers():
                denied = _firewall_missing(ctx.event)
                if denied is not None:
                    return denied
        return core.dispatch(ctx)
    except Exception as exc:  # noqa: BLE001 - mai bloccare la sessione per un bug
        core.log_error("dispatch", exc)
        # Nel profilo isolato un errore a runtime chiude: nessuna chiamata passa senza guardie
        if event_arg == "PreToolUse" and _env_non_default():
            sys.stderr.write(
                f"GUARD-BLOCK [profile] fail closed: the hook dispatcher failed under a non-default "
                f"profile ({type(exc).__name__}); see the hooks log\n"
            )
            return 2
        return 0


if __name__ == "__main__":
    sys.exit(main())
