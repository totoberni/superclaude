"""Decisione on/off del flag /git (`config/git-policy`): unica fonte della regola.

Funzione pura, solo libreria standard, importabile con `python -S`. La chiamano la
guardia git-policy degli hook e il motore di sync, ognuno con il percorso del flag del
proprio profilo (`sc_profile.git_policy_path(profilo)`).

Il modulo non legge MAI variabili d'ambiente: un override del percorso via ambiente
sarebbe un aggiramento di /git false in un solo token, perche' un agente controlla
l'ambiente dei propri comandi. Solo la guardia degli hook, che legge l'ambiente della
sessione, risolve il proprio override e passa qui il percorso gia' scelto.
"""
from __future__ import annotations

from pathlib import Path

# Parole riconosciute nel flag, confrontate in minuscolo dopo strip e BOM UTF-8
DISABLED_WORDS = ("disabled", "false", "off", "0", "no")
ENABLED_WORDS = ("enabled", "true", "on", "1", "yes")


def git_disabled(policy_path: str | Path, fail_closed: bool) -> bool:
    """True se il flag in `policy_path` blocca git.

    fail_closed=False (profilo superclaude): blocca solo un flag leggibile che dice
    `disabled` (o `false`, `off`, `0`, `no`); assente, illeggibile o ignoto = abilitato.
    fail_closed=True (altri profili): sblocca solo un flag leggibile esplicitamente
    abilitante (`enabled`, `true`, `on`, `1`, `yes`); assente, illeggibile o ignoto = bloccato.
    """
    try:
        word = Path(policy_path).read_text(encoding="utf-8-sig", errors="replace").strip().lower()
    except (OSError, ValueError):
        return fail_closed
    if fail_closed:
        return word not in ENABLED_WORDS
    return word in DISABLED_WORDS
