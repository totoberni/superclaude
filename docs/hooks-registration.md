# Registrazione degli hook

Fonte di verita': `hooks/hooks.json`. Lo applica `bin/apply-settings.ps1 -Profile superclaude|deepseek` (script `scripts/settings/apply_settings.py`), che sostituisce i segnaposto `{{PYTHON}}` (interprete di base Python 3.12) e `{{DISPATCH}}` (`hooks/dispatch.py`).

## Forma exec

Con `args` impostato il comando parte senza shell: ogni elemento e' un argomento, niente Git Bash, niente espansione di variabili. Su Windows `command` deve essere un vero .exe, e python.exe lo e'. Restano il flag `-S` (niente site-packages) e l'interprete di base, non quello del venv.

## Fallimento all'importazione

Sotto un profilo non predefinito (deepseek) un errore di importazione del nucleo degli hook o di profile-firewall NEGA ogni PreToolUse (exit 2); gli altri eventi escono con 1 e un traceback, senza bloccare (SessionStart, per esempio, non inietta il puntatore alla memoria). Sotto superclaude resta il comportamento precedente: exit 1, non bloccante.

## Tabella delle decisioni sui filtri `if`

Regola: `if` e' valutato PRIMA di avviare il processo e SOLO sugli eventi strumento (PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, PermissionDenied). Un solo processo serve tutti gli handler di un evento, quindi un filtro vale per l'intero gruppo: puo' essere aggiunto soltanto dove ogni handler raggiunto da quella entry lo accetta. Nessun filtro puo' restringere una guardia che nega o riscrive (write-ACL, git-policy, commit-gate, content-scan, notebook-guard, key-guard, profile-firewall) ne' baseline-stash, il cui effetto collaterale serve a ogni scrittura (R-2).

| Evento | Matcher | if | Handler serviti | Decisione |
|---|---|---|---|---|
| SessionStart | "" | nessuno | memory-inject | Fuori dagli eventi strumento un `if` non viene ignorato: disattiverebbe l'hook, e memory-inject smetterebbe di partire. Vietato. |
| SubagentStart | "" | nessuno | memory-inject | come sopra. |
| PreToolUse | Bash\|PowerShell\|Monitor\|Write\|Edit\|MultiEdit\|NotebookEdit\|Agent\|Read\|Glob\|Grep | nessuno | profile-firewall, write-acl, content-scan, notebook-guard, key-guard, commit-gate, git-policy, git-verb, cd-note, baseline-stash, spawn-log, thinking-nudge, context-check | Qui vivono tutte le guardie. Monitor e' nel matcher perche' esegue comandi come Bash e PowerShell: e' in `core.SHELL_TOOLS`, quindi lo vedono gli stessi handler dei comandi shell, tranne cd-note, che lo salta (il cd di un Monitor non sposta la shell della sessione). Monitor e' disponibile in entrambi i profili: nessuna regola nativa lo nega e il firewall tra profili ne filtra il comando come un comando shell. Le regole native `Bash(...)` (deny e ask) valgono anche per i comandi di Monitor, le regole `PowerShell(...)` no (tools-reference.md, tabella dei formati di regola righe 82-83 e § Monitor tool riga 382). Su Windows Monitor esiste solo se e' installato Git Bash, e non e' disponibile quando e' impostato `DISABLE_TELEMETRY` o `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` (tools-reference.md § Monitor tool riga 386): per questo l'env del profilo DeepSeek non imposta nessuna delle due variabili (verificato da `scripts/tests/test_permission_rules.py` e `scripts/tests/test_apply_settings.py`). Read, Glob e Grep sono nel matcher solo per profile-firewall (registrato senza `tools`) e key-guard (che li dichiara); gli altri handler dichiarano i propri strumenti e su Read/Glob/Grep non partono (verificato da `hooks/tests/test_registration.py`). Un `if` come "Bash(git *)" spegnerebbe le guardie sugli altri comandi: vietato. |
| PostToolUse (1) | Agent | nessuno | spawn-log, worker-verify | Il payload e' un sottoagente, non un file: non esiste una regola di permesso che lo descriva. |
| PostToolUse (2) | Edit\|MultiEdit | Edit(//**/*.tex) | latex-warn | latex-warn e' l'unico handler dell'evento per questi strumenti e la sua prima riga esce se il percorso non finisce in .tex, quindi il filtro fa partire il processo sui file .tex in minuscolo e lo risparmia sulle modifiche non .tex. L'handler confronta l'estensione senza distinguere maiuscole e minuscole, mentre la documentazione non dice se i pattern di percorso lo fanno: un file .TEX potrebbe non far partire l'hook. Il prefisso //**/ serve: un pattern nudo come Edit(*.tex) e' ancorato alla directory di lavoro corrente. Verificato dal vivo (2026-10-13): Edit su un .tex fa partire l'hook. |
| PostToolUse (3) | Write | nessuno | latex-warn | Documentazione e prova dal vivo sono in contraddizione: la tabella dei formati di regola (tools-reference.md righe 76 e 85) dice che `Edit(...)` copre Edit, Write e NotebookEdit anche nel campo `if` di un hook, ma la prova dal vivo su Claude Code 2.1.296 del 2026-10-13 ha mostrato che il filtro Edit(//**/*.tex) non e' scattato su una Write. Con entrambe le letture Write resta senza filtro, in una entry propria. Un filtro Write(...) non e' affidabile: la documentazione dei permessi dice che le regole di percorso su Write sono accettate ma mai consultate (si usa Edit(...)). Senza filtro il processo parte a ogni Write e latex-warn esce subito sui percorsi non .tex. `hooks/tests/test_registration.py` rifiuta un filtro Edit(...) su un matcher che accetta Write e ogni filtro Write(...), MultiEdit(...), NotebookEdit(...) o Glob(...). |
| SubagentStop | "" | nessuno | spawn-log | evento non strumento. |
| PreCompact | "" | nessuno | snapshots | evento non strumento. |
| Stop | "" | nessuno | snapshots | evento non strumento. La registrazione si puo' omettere con `-DropStop`. |
| SessionEnd | "" | nessuno | session-cleanup | evento non strumento. |

La corrispondenza fra registro degli handler e `hooks.json` e' verificata da `hooks/tests/test_registration.py`: se un handler nuovo resta scoperto, o se una guardia finisce dietro un filtro, il test fallisce.

## Verifica empirica dopo l'applicazione

Riavviare prima Claude Code. latex-warn tace finche' accanto al .tex non c'e' un log TeX, e al primo avvistamento di un log semina la cache in silenzio: la prova quindi prepara un log e osserva la cache.

1. In una cartella di prova creare `probe.log` con la sola riga `This is pdfTeX, Version 3.141592653`.
2. Far creare all'agente, con lo strumento Write, un NUOVO file `probe.tex` nella stessa cartella: in `.latex-warn-cache` (sotto la data home del profilo) deve comparire un file nuovo. Se non compare, la entry PostToolUse (3) non e' attiva.
3. Aggiungere al log una riga `LaTeX Warning: Reference ... on page 1 undefined on input line 1.` e far modificare `probe.tex` con Edit: deve comparire la nota di latex-warn.
4. Una Write su un file .py non deve produrre alcuna nota ne' file nuovi nella cache.

La procedura passo per passo per il profilo superclaude e' il passo A4 di `plans/deepseek/smoke-test.md`.
