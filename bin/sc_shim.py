"""Launcher condiviso per gli shim `mem` e `scpy` (Windows e Git Bash).

Unico punto in cui vivono i percorsi di superclaude: gli skill e le regole
chiamano solo `mem ...` e `scpy ...`, mai l'interprete del venv direttamente.

Uso:
    sc_shim.py mem <sottocomando> [argomenti...]
    sc_shim.py scpy <script relativo a ~/.claude/scripts> [argomenti...]
    sc_shim.py scpy -m <modulo> [argomenti...]   (passa tutto all'interprete)
"""
from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

CLAUDE_HOME = Path.home() / ".claude"
SCRIPTS_DIR = CLAUDE_HOME / "scripts"
MEMORY_CLI = SCRIPTS_DIR / "memory" / "memory_db.py"

# Sottocomandi che accettano lo slug come primo argomento posizionale
# (compatibilità con il vecchio wrapper bash: `mem get <nome>`).
NAME_SHORTHAND = {"get", "similar"}


def _force_utf8() -> None:
    """Evita UnicodeEncodeError sulle console Windows con code page legacy."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _offline_env() -> None:
    """Embedding sempre offline, salvo richiesta esplicita (primo download)."""
    # Senza privilegi admin Windows non crea symlink: la cache HF funziona lo stesso
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    if os.environ.get("SC_ONLINE") == "1":
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
        return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def _run_script(script: Path, args: list[str]) -> int:
    if not script.is_file():
        print(f"sc_shim: script non trovato: {script}", file=sys.stderr)
        return 2
    # Lo script deve poter importare i moduli che gli stanno accanto
    sys.path.insert(0, str(script.parent))
    sys.argv = [str(script), *args]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0


def _mem(args: list[str]) -> int:
    if args and args[0] in NAME_SHORTHAND and len(args) > 1 and not args[1].startswith("-"):
        args = [args[0], "--name", args[1], *args[2:]]
    return _run_script(MEMORY_CLI, args)


def _scpy(args: list[str]) -> int:
    if not args:
        print("uso: scpy <script.py> [argomenti...] | scpy -m <modulo> ...", file=sys.stderr)
        return 2
    if args[0].startswith("-"):
        # Opzioni dell'interprete (-m, -c, ...): delega a un processo figlio
        return subprocess.call([sys.executable, *args])
    target = Path(args[0]).expanduser()
    if not target.is_absolute():
        target = SCRIPTS_DIR / target
    return _run_script(target, args[1:])


def _expand_home(args: list[str]) -> list[str]:
    """PowerShell non espande `~` per gli eseguibili nativi: lo facciamo qui.

    Tocca solo gli argomenti che iniziano con `~/` o `~\\` (mai il testo libero
    di una query), anche nella forma `--opzione=~/percorso`.
    """
    home = str(Path.home())
    out: list[str] = []
    for arg in args:
        head, sep, tail = arg.partition("=")
        if arg.startswith(("~/", "~\\")):
            arg = home + arg[1:]
        elif sep and head.startswith("--") and tail.startswith(("~/", "~\\")):
            arg = f"{head}={home}{tail[1:]}"
        out.append(arg)
    return out


def main() -> int:
    _force_utf8()
    _offline_env()
    if len(sys.argv) < 2 or sys.argv[1] not in {"mem", "scpy"}:
        print("uso: sc_shim.py {mem|scpy} ...", file=sys.stderr)
        return 2
    mode, rest = sys.argv[1], _expand_home(sys.argv[2:])
    return _mem(rest) if mode == "mem" else _scpy(rest)


if __name__ == "__main__":
    sys.exit(main())
