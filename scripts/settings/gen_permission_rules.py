"""Generatore delle regole native deny/ask di permission-rules.json (fonte unica).

Uso:
    python gen_permission_rules.py --check   # exit 1 se deny/ask del JSON differiscono dal generato
    python gen_permission_rules.py --write   # riscrive deny/ask nel JSON (mantiene _comment)

Famiglie di regole
------------------
deny (cancellazioni dirette a bersagli protetti, dischi):
  - Bash rm: bersaglio protetto (radice, /c, C:/, /c/Users, ~, ~/code) in qualunque posizione, anche
    dopo i flag (forma "rm* T" e "rm* T *"); bersagli con prefisso variabile ($HOME, $USERPROFILE,
    /Users/<utente>, /Users/*/code, /.claude...) con forma "rm *T" e "rm *T *".
  - PowerShell Remove-Item: stessa logica, con tre forme di virgolette (nessuna, doppie, singole) e
    separatori barra rovescia o barra normale (radice C:, C:\\Users, ~, home assoluta, ~/code...).
  - cmd rd/rmdir/del/erase verso la radice C:, C:\\Users o la home assoluta.
  - Strumenti di disco (format, diskpart, bcdedit, vssadmin, cipher, anche con suffisso .exe;
    cmdlet di partizionamento).
ask (operazioni distruttive ma legittime):
  - rm/Remove-Item ricorsivi, find con -delete o -exec/-execdir rm, xargs che lancia rm,
    rd/rmdir/del/erase con /s, comandi git distruttivi (anche con opzioni globali prima del verbo),
    SQL distruttivo passato a client noti.

Semantica del matcher di Claude Code
------------------------------------
  - Documentato in permissions.md: confronto ancorato su ogni sottocomando, * = qualunque testo,
    PowerShell senza distinzione tra maiuscole e minuscole, Bash sensibile (da qui le varianti
    maiuscole e minuscole dello SQL).
  - NON documentato in permissions.md (r9): barra rovescia prima di * = asterisco letterale; doppia
    barra rovescia = una barra rovescia letterale. Fonte: lettura del sorgente della CLI
    (claude.exe 2.1.293, funzione di matching delle regole) nella revisione del primo passaggio,
    plans/deepseek/hostile-review-2.md, esecuzione R3.

Limiti noti
-----------
  - Le forme con variabili ($HOME, $USERPROFILE) o con percorsi ottenuti a runtime non sono
    risolte: la regola copre solo il testo letterale del comando.
  - Il matcher non sa saltare il testo tra virgolette: "git * <verbo>" scatta anche su un messaggio
    di commit che nomina il verbo (git commit -m "... reset --hard"), e in PowerShell
    "*python*TRUNCATE *" scatta anche su flag come --truncate 80. Sovra-corrispondenza accettata
    (solo ask), fissata come caso atteso nei test.
  - La home assoluta usa il segnaposto {{HOME_USER}}: apply_settings.py lo sostituisce al momento
    dell'applicazione con il nome utente della macchina (Path.home().name); la home di un altro
    utente non e coperta in forma esatta.
  - Le forme "Users/*/code" negano anche la cancellazione della cartella code di altri utenti:
    accettato su un portatile con un solo utente.
  - Le pipeline PowerShell (Get-ChildItem | Remove-Item senza -Recurse) non sono coperte.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RULES_FILE = Path(__file__).resolve().parent / "permission-rules.json"

BS = "\\\\"    # barra rovescia letterale nel pattern (due caratteri)
LSTAR = "\\*"  # asterisco letterale nel pattern (due caratteri)

# Segnaposto e non derivato da Path.home(): il file generato resta identico su ogni macchina,
# il nome utente lo inserisce l'applicatore al momento dell'applicazione
HOME_USER = "{{HOME_USER}}"


# ---------------------------------------------------------------- helper di composizione

def shapes(cmd: str, targets: list[str], leading: bool) -> list[str]:
    """Per ogni bersaglio produce la forma nuda e quella con argomenti successivi.

    leading=True: bersaglio con prefisso variabile ("cmd *T"); False: bersaglio esatto ("cmd* T").
    """
    out: list[str] = []
    for t in targets:
        if leading:
            out += [f"{cmd} *{t}", f"{cmd} *{t} *"]
        else:
            out += [f"{cmd}* {t}", f"{cmd}* {t} *"]
    return out


def closings(bases: list[str], ends: list[str]) -> list[str]:
    """Prodotto base x finale, nell'ordine bases poi ends."""
    return [b + e for b in bases for e in ends]


# ---------------------------------------------------------------- Bash: bersagli di rm

BASH_EXACT_TARGETS = [
    "/", "/" + LSTAR, '"/"', "'/'",
    "/c", "/c/", "/c/" + LSTAR, '"/c"', '"/c/"', "'/c'", "'/c/'",
    "~", "~/", "~/" + LSTAR, "~/code", "~/code/", "~/code/" + LSTAR,
    "C:/", "C:/" + LSTAR, '"C:/"', "'C:/'",
    "/c/Users", "/c/Users/", "/c/Users/" + LSTAR, "C:/Users", "C:/Users/", "C:/Users/" + LSTAR,
]

BASH_HOME_BASES = ["$HOME", "${HOME}", "$HOME/code", "${HOME}/code",
                   "$USERPROFILE", "${USERPROFILE}", "$USERPROFILE/code", "${USERPROFILE}/code"]
BASH_HOME_ENDINGS = ["", "/", "/" + LSTAR, '"', '/"']

BASH_PATH_BASES = ["/Users/*/code", "/Users/" + HOME_USER, "/.claude", "/.claude-deepseek",
                   "/.claude/agent-memory", "/.claude-deepseek/agent-memory"]
BASH_PATH_ENDINGS = ["", "/", "/" + LSTAR, '"', '/"', "'", "/'"]

# Home assoluta con barre rovesce: in Bash e pericolosa solo tra virgolette (senza, la shell le toglie)
BASH_BS_BASES = ["Users" + BS + HOME_USER]
BASH_BS_ENDINGS = ["", BS, BS + LSTAR, '"', "'", BS + "'"]

# ---------------------------------------------------------------- PowerShell: bersagli di Remove-Item

QUOTES_ALL = ("", '"', "'")   # nessuna, doppie, singole
QUOTES_NO_SINGLE = ("", '"')  # le variabili non si espandono tra apici singoli

# Finali dopo la base: nessuno, barra rovescia, barra normale, e le varianti con asterisco
SEP_ENDS = ["", BS, "/", BS + LSTAR, "/" + LSTAR]

# Radici di disco e home esatte: "C:" richiede un separatore (SEP_ENDS senza il caso vuoto)
PS_EXACT_ROOTS = (("C:", SEP_ENDS[1:]), ("~", SEP_ENDS),
                  ("C:" + BS + "Users", SEP_ENDS), ("C:/Users", SEP_ENDS))
PS_EXACT_EXTRA = [BS, "/"]
PS_EXACT_TARGETS = [q + root + e + q
                    for q in QUOTES_ALL
                    for root, ends in PS_EXACT_ROOTS
                    for e in ends] + PS_EXACT_EXTRA

PS_ENDS = [e + q for q in QUOTES_ALL for e in SEP_ENDS]
PS_ENDS_DOLLAR = [e + q for q in QUOTES_NO_SINGLE for e in SEP_ENDS]

# Basi con variabile ($HOME ...): niente forme con apici singoli
PS_DOLLAR_BASES = ["$HOME", "$env:USERPROFILE",
                   "$HOME" + BS + "code", "$HOME/code",
                   "$env:USERPROFILE" + BS + "code", "$env:USERPROFILE/code"]
# Basi letterali, con entrambe le forme di separatore
PS_LITERAL_BASES = ["~" + BS + "code", "~/code",
                    "Users" + BS + "*" + BS + "code", "Users/*/code",
                    "Users" + BS + HOME_USER, "Users/" + HOME_USER,
                    BS + ".claude", "/.claude",
                    BS + ".claude-deepseek", "/.claude-deepseek",
                    BS + ".claude" + BS + "agent-memory", "/.claude/agent-memory",
                    BS + ".claude-deepseek" + BS + "agent-memory", "/.claude-deepseek/agent-memory"]

# ---------------------------------------------------------------- strumenti di disco

DISK_BASH = ["format *", "diskpart *", "vssadmin delete *", "bcdedit */set*", "bcdedit */delete*",
             "bcdedit */import*", "cipher */w*"]
DISK_PS = ["format *", "diskpart *", "Format-Volume *", "Clear-Disk *", "Initialize-Disk *",
           "Remove-Partition *", "vssadmin delete *", "bcdedit */set*", "bcdedit */delete*",
           "bcdedit */import*", "cipher */w*"]
DISK_EXE_TOOLS = ("format", "diskpart", "vssadmin", "bcdedit", "cipher")


def with_exe(rules: list[str]) -> list[str]:
    """Aggiunge la grafia .exe di ogni regola che inizia con uno strumento di disco."""
    exe = []
    for r in rules:
        head, _, rest = r.partition(" ")
        if head in DISK_EXE_TOOLS:
            exe.append(f"{head}.exe {rest}")
    return rules + exe


# ---------------------------------------------------------------- cmd: bersagli catastrofici (deny)

CMD_VERBS = ("rd", "rmdir", "del", "erase")
CMD_ROOTS = (("C:", [BS, BS + LSTAR]),
             ("C:" + BS + "Users", ["", BS, BS + LSTAR]),
             ("C:" + BS + "Users" + BS + HOME_USER, ["", BS, BS + LSTAR]))
CMD_TARGETS = [root + e for root, ends in CMD_ROOTS for e in ends]
# Forme: bersaglio finale, seguito da altri argomenti, o chiuso da virgolette (cmd //c "rd ... T")
CMD_DENY_RULES = [r for v in CMD_VERBS for t in CMD_TARGETS
                  for r in (f"cmd*{v} *{t}", f"cmd*{v} *{t} *", f'cmd*{v} *{t}"')]

# ---------------------------------------------------------------- famiglie ask

GIT_VERBS = ["reset --hard*", "clean -*f*", "push -f*", "push * -f*", "push *--force*", "push * +*",
             "branch -D*", "branch * -D*", "branch *--force*", "checkout -- .", "checkout * -- .",
             "checkout .", "restore .", "stash drop*", "stash clear*", "filter-branch*"]
GIT_RULES = [f"git {v}" for v in GIT_VERBS] + [f"git * {v}" for v in GIT_VERBS]  # anche con opzioni globali
CMD_SHELL_RULES = [f"cmd*{v} */s*" for v in CMD_VERBS]
# Cancellazioni ricorsive che non iniziano con rm (xargs con flag non viene tolto dal matcher)
FIND_XARGS_ASK = ["find * -delete*", "find * -exec rm *", "find * -execdir rm *", "xargs * rm *"]

SQL_CLIENTS = ["sqlite3", "psql", "duckdb", "mysql", "python"]
SQL_UPPER = ["DROP TABLE", "DROP DATABASE", "DROP SCHEMA", "TRUNCATE "]
SQL_LOWER = ["drop table", "drop database", "drop schema", "truncate table"]

RM_ASK = ["rm -r*", "rm -R*", "rm -fr*", "rm -fR*", "rm --recursive*",
          "rm * -r*", "rm * -R*", "rm * -fr*", "rm * -fR*", "rm * --recursive*"]
PS_REMOVE_ASK = ["Remove-Item -r*", "Remove-Item * -r*"]


def sql_rules(patterns: list[str]) -> list[str]:
    return [f"*{c}*{p}*" for c in SQL_CLIENTS for p in patterns]


# ---------------------------------------------------------------- assemblaggio

def build_deny() -> list[str]:
    bash = (with_exe(DISK_BASH) + CMD_DENY_RULES
            + shapes("rm", BASH_EXACT_TARGETS, False)
            + shapes("rm", closings(BASH_HOME_BASES, BASH_HOME_ENDINGS)
                     + closings(BASH_PATH_BASES, BASH_PATH_ENDINGS)
                     + closings(BASH_BS_BASES, BASH_BS_ENDINGS), True))
    ps = (with_exe(DISK_PS) + CMD_DENY_RULES
          + shapes("Remove-Item", PS_EXACT_TARGETS, False)
          + shapes("Remove-Item", closings(PS_DOLLAR_BASES, PS_ENDS_DOLLAR), True)
          + shapes("Remove-Item", closings(PS_LITERAL_BASES, PS_ENDS), True))
    return [f"Bash({r})" for r in bash] + [f"PowerShell({r})" for r in ps]


def build_ask() -> list[str]:
    bash = RM_ASK + FIND_XARGS_ASK + CMD_SHELL_RULES + GIT_RULES + sql_rules(SQL_UPPER + SQL_LOWER)
    ps = PS_REMOVE_ASK + FIND_XARGS_ASK + CMD_SHELL_RULES + GIT_RULES + sql_rules(SQL_UPPER)
    return [f"Bash({r})" for r in bash] + [f"PowerShell({r})" for r in ps]


def generate() -> dict[str, list[str]]:
    deny, ask = build_deny(), build_ask()
    assert len(deny) == len(set(deny)) and len(ask) == len(set(ask)), "regole duplicate"
    return {"deny": deny, "ask": ask}


def render(existing: dict, generated: dict[str, list[str]]) -> str:
    doc = {"_comment": existing["_comment"], **generated}
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    existing = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    generated = generate()
    if args.check:
        same = all(existing.get(k) == generated[k] for k in generated)
        print(f"deny={len(generated['deny'])} ask={len(generated['ask'])} "
              f"{'OK' if same else 'DIFFERENT'}")
        return 0 if same else 1
    with RULES_FILE.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(existing, generated))
    print(f"scritto {RULES_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
