# Registrazione degli hook

Fonte di verita': `hooks/hooks.json`. Lo applica `bin/apply-settings.ps1 -Profile superclaude|deepseek` (script `scripts/settings/apply_settings.py`), che sostituisce i segnaposto `{{PYTHON}}` (interprete di base Python 3.12) e `{{DISPATCH}}` (`hooks/dispatch.py`).

## Forma exec

Con `args` impostato il comando parte senza shell: ogni elemento e' un argomento, niente Git Bash, niente espansione di variabili. Su Windows `command` deve essere un vero .exe, e python.exe lo e'. Restano il flag `-S` (niente site-packages) e l'interprete di base, non quello del venv.

## Fallimento all'importazione

Sotto un profilo non predefinito (deepseek) un errore di importazione del nucleo degli hook o di profile-firewall NEGA ogni PreToolUse (exit 2); gli altri eventi escono con 1 e un traceback, senza bloccare (SessionStart, per esempio, non inietta il puntatore alla memoria). Sotto superclaude resta il comportamento precedente: exit 1, non bloccante.

## Tabella delle decisioni sui filtri `if`

Regola: `if` e' valutato PRIMA di avviare il processo e SOLO sugli eventi strumento (PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, PermissionDenied). Un solo processo serve tutti gli handler di un evento, quindi un filtro vale per l'intero gruppo: puo' essere aggiunto soltanto dove ogni handler raggiunto da quella entry lo accetta. Nessun filtro puo' restringere una guardia che nega o riscrive (write-ACL, git-policy, commit-gate, content-scan, notebook-guard, profile-firewall) ne' baseline-stash, il cui effetto collaterale serve a ogni scrittura (R-2).

| Evento | Matcher | if | Handler serviti | Decisione |
|---|---|---|---|---|
| SessionStart | "" | nessuno | memory-inject | Fuori dagli eventi strumento un `if` non viene ignorato: disattiverebbe l'hook, e memory-inject smetterebbe di partire. Vietato. |
| SubagentStart | "" | nessuno | memory-inject | come sopra. |
| PreToolUse | Bash\|PowerShell\|Monitor\|Write\|Edit\|MultiEdit\|NotebookEdit\|Agent\|Read\|Glob\|Grep | nessuno | profile-firewall, write-acl, content-scan, notebook-guard, commit-gate, git-policy, git-verb, cd-note, baseline-stash, spawn-log, thinking-nudge, context-check | Qui vivono tutte le guardie. Monitor e' nel matcher perche' esegue comandi come Bash e PowerShell: e' in `core.SHELL_TOOLS`, quindi lo vedono gli stessi handler dei comandi shell, tranne cd-note, che lo salta (il cd di un Monitor non sposta la shell della sessione). Nel profilo deepseek Monitor e' anche negato dalle regole native. Read, Glob e Grep sono nel matcher solo per profile-firewall (registrato senza `tools`); gli altri handler dichiarano i propri strumenti e su Read/Glob/Grep non partono (verificato da `hooks/tests/test_registration.py`). Un `if` come "Bash(git *)" spegnerebbe le guardie sugli altri comandi: vietato. |
| PostToolUse (1) | Agent | nessuno | spawn-log, worker-verify | Il payload e' un sottoagente, non un file: non esiste una regola di permesso che lo descriva. |
| PostToolUse (2) | Write\|Edit\|MultiEdit | Edit(//**/*.tex) | latex-warn | latex-warn e' l'unico handler dell'evento per questi strumenti e la sua prima riga esce se il percorso non finisce in .tex. Secondo la documentazione le regole Edit(...) sono consultate anche per Write e MultiEdit (le regole Write(...) mai), quindi il filtro coincide con il controllo dell'handler: affermazione verificata solo sulla documentazione, da confermare con la verifica empirica qui sotto. Il prefisso //**/ serve: un pattern nudo come Edit(*.tex) e' ancorato alla directory di lavoro corrente. |
| SubagentStop | "" | nessuno | spawn-log | evento non strumento. |
| PreCompact | "" | nessuno | snapshots | evento non strumento. |
| Stop | "" | nessuno | snapshots | evento non strumento. La registrazione si puo' omettere con `-DropStop`. |
| SessionEnd | "" | nessuno | session-cleanup | evento non strumento. |

La corrispondenza fra registro degli handler e `hooks.json` e' verificata da `hooks/tests/test_registration.py`: se un handler nuovo resta scoperto, o se una guardia finisce dietro un filtro, il test fallisce.

## Verifica empirica dopo l'applicazione

Riavviare prima Claude Code. latex-warn tace finche' accanto al .tex non c'e' un log TeX, e al primo avvistamento di un log semina la cache in silenzio: la prova quindi prepara un log e osserva la cache.

1. In una cartella di prova creare `probe.log` con la sola riga `This is pdfTeX, Version 3.141592653`.
2. Far creare all'agente, con lo strumento Write, un NUOVO file `probe.tex` nella stessa cartella: in `.latex-warn-cache` (sotto la data home del profilo) deve comparire un file nuovo. Se non compare, il filtro `Edit(...)` non copre Write e la tabella va corretta.
3. Aggiungere al log una riga `LaTeX Warning: Reference ... on page 1 undefined on input line 1.` e far modificare `probe.tex` con Edit: deve comparire la nota di latex-warn.
4. Una Write su un file .py non deve produrre alcuna nota ne' file nuovi nella cache.

La procedura passo per passo per il profilo superclaude e' il passo A4 di `plans/deepseek/smoke-test.md`.
