# =====================================================================
# superclaude / Windows 11 (U-1): registrazione degli hook in forma exec.
#
#   .\apply-hooks-v2.ps1             applica (con backup datato)
#   .\apply-hooks-v2.ps1 -DryRun     mostra il JSON risultante senza scrivere
#   .\apply-hooks-v2.ps1 -Revert     ripristina l'ultimo backup
#   .\apply-hooks-v2.ps1 -Force      sovrascrive una sezione 'hooks' non riconosciuta
#   .\apply-hooks-v2.ps1 -DropStop   omette la registrazione di Stop (voce H-1)
#
# Compatibile con Windows PowerShell 5.1 e PowerShell 7. Idempotente.
# Tocca SOLO la chiave 'hooks' di settings.json: env, permissions, statusLine,
# model e tutto il resto restano come sono.
#
# La registrazione vera e propria sta in ~\.claude\hooks\hooks.json (unica fonte
# di verita'); qui si sostituiscono solo i due segnaposto {{PYTHON}} e
# {{DISPATCH}} con i percorsi assoluti di questa macchina.
#
# ---------------------------------------------------------------------
# Forma exec
# ---------------------------------------------------------------------
# Con 'args' impostato il comando parte senza shell: ogni elemento e' un
# argomento, niente Git Bash, niente espansione di variabili. Su Windows
# 'command' deve essere un vero .exe, e python.exe lo e'. Restano il flag -S
# (niente site-packages) e l'interprete di base, non quello del venv.
#
# ---------------------------------------------------------------------
# Tabella delle decisioni sui filtri 'if'
# ---------------------------------------------------------------------
# Regola: `if` e' valutato PRIMA di avviare il processo e SOLO sugli eventi
# strumento (PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest,
# PermissionDenied). Un solo processo serve tutti gli handler di un evento,
# quindi un filtro vale per l'intero gruppo: puo' essere aggiunto soltanto dove
# ogni handler raggiunto da quella entry lo accetta. Nessun filtro puo'
# restringere una guardia che nega o riscrive (write-ACL, git-policy,
# commit-gate, content-scan, notebook-guard) ne' baseline-stash, il cui effetto
# collaterale serve a ogni scrittura (R-2).
#
# | Evento             | Matcher                                                | if           | Handler serviti                                                                                        | Decisione |
# |--------------------|--------------------------------------------------------|--------------|--------------------------------------------------------------------------------------------------------|-----------|
# | SessionStart       | ""                                                     | nessuno      | memory-inject                                                                                            | `if` non viene valutato fuori dagli eventi strumento: sarebbe lettera morta. |
# | SubagentStart      | ""                                                     | nessuno      | memory-inject                                                                                            | come sopra. |
# | PreToolUse         | Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|Agent | nessuno      | write-acl, content-scan, notebook-guard, commit-gate, git-policy, git-verb, baseline-stash, spawn-log, thinking-nudge, context-check | Qui vivono tutte le guardie. context-check e' registrato senza `tools`, quindi deve vedere ogni strumento del matcher. Un `if` come "Bash(git *)" salverebbe uno spawn ma spegnerebbe notebook-guard e commit-gate sugli altri comandi: vietato. |
# | PostToolUse (1)    | Agent                                                  | nessuno      | spawn-log, worker-verify                                                                                 | Il payload e' un sottoagente, non un file: non esiste una regola di permesso che lo descriva. |
# | PostToolUse (2)    | Write|Edit|MultiEdit                                   | Edit(//**/*.tex) | latex-warn                                                                                           | latex-warn e' l'unico handler dell'evento per questi strumenti e la sua prima riga esce se il percorso non finisce in .tex (handlers/latex_warn.py). Le regole Edit(...) sono quelle consultate anche per Write e MultiEdit (le regole Write(...) non vengono mai consultate), quindi il filtro coincide con il controllo dell'handler: nessuna perdita di copertura, e lo spawn sparisce da ogni modifica non LaTeX. Il prefisso //**/ e' necessario: un pattern nudo come Edit(*.tex) e' ancorato alla directory di lavoro corrente e mancherebbe i .tex altrove (su Windows i percorsi si normalizzano in /c/Users/..., e //**/ attraversa tutte le unita'). Questa riga supera la voce H-2 (spostare latex-warn su PreToolUse non serve piu'). |
# | SubagentStop       | ""                                                     | nessuno      | spawn-log                                                                                                | evento non strumento. |
# | PreCompact         | ""                                                     | nessuno      | snapshots                                                                                                | evento non strumento. |
# | Stop               | ""                                                     | nessuno      | snapshots                                                                                                | evento non strumento. Resta registrato: la rimozione e' la voce H-1, qui disponibile con -DropStop. |
# | SessionEnd         | ""                                                     | nessuno      | session-cleanup                                                                                          | evento non strumento. |
#
# La corrispondenza fra registro degli handler e hooks.json e' verificata da
# hooks\tests\test_registration.py: se un handler nuovo resta scoperto, o se una
# guardia finisce dietro un filtro, il test fallisce.
#
# Verifica empirica dopo l'applicazione (riavviare prima Claude Code): una Write
# su un file .py non deve avviare alcun processo PostToolUse, mentre una modifica
# a un file .tex deve continuare a produrre la nota di latex-warn.
# =====================================================================
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Revert,
    [switch]$Force,
    [switch]$DropStop,     # omette la registrazione di Stop (voce H-1)
    [string]$HooksFile     # sorgente alternativa (predefinita: ~\.claude\hooks\hooks.json)
)
$ErrorActionPreference = 'Stop'

$ClaudeHome = Join-Path $env:USERPROFILE '.claude'
$Settings   = Join-Path $ClaudeHome 'settings.json'
$HooksJson  = if ($HooksFile) { $HooksFile } else { Join-Path $ClaudeHome 'hooks\hooks.json' }
$BasePython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
$Dispatch   = Join-Path $ClaudeHome 'hooks\dispatch.py'
$Utf8NoBom  = New-Object System.Text.UTF8Encoding($false)

function ConvertTo-Posix([string]$p) { $p -replace '\\', '/' }

# ---------------------------------------------------------------------
# Ripristino
# ---------------------------------------------------------------------
if ($Revert) {
    $last = Get-ChildItem $ClaudeHome -Filter 'settings.json.bak-*' | Sort-Object Name | Select-Object -Last 1
    if (-not $last) { throw "Nessun backup trovato in $ClaudeHome" }
    # Un backup troncato o non valido renderebbe settings.json illeggibile
    try { $null = (Get-Content $last.FullName -Raw | ConvertFrom-Json) }
    catch { throw "Il backup $($last.Name) non e' JSON valido: ripristino annullato. $($_.Exception.Message)" }
    Copy-Item $last.FullName $Settings -Force
    Write-Host "Ripristinato: $($last.Name)" -ForegroundColor Green
    return
}

# ---------------------------------------------------------------------
# Controlli preliminari
# ---------------------------------------------------------------------
foreach ($need in $BasePython, $Dispatch, $HooksJson) {
    if (-not (Test-Path $need)) { throw "Prerequisito mancante: $need" }
}

# ---------------------------------------------------------------------
# hooks.json: sostituzione dei segnaposto
# ---------------------------------------------------------------------
$raw = Get-Content $HooksJson -Raw
foreach ($token in '{{PYTHON}}', '{{DISPATCH}}') {
    if ($raw -notmatch [regex]::Escape($token)) { throw "hooks.json non contiene il segnaposto $token" }
}
$raw = $raw.Replace('{{PYTHON}}', (ConvertTo-Posix $BasePython)).Replace('{{DISPATCH}}', (ConvertTo-Posix $Dispatch))

$hooks = $raw | ConvertFrom-Json
$hooks.PSObject.Properties.Remove('_comment')     # commento della sorgente, non della configurazione
if ($DropStop) {
    $hooks.PSObject.Properties.Remove('Stop')
    Write-Host "Stop omesso (-DropStop): lo snapshot per sessione resta a carico di PreCompact." -ForegroundColor Yellow
}

# Ogni comando deve essere in forma exec e puntare a un .exe esistente
foreach ($event in $hooks.PSObject.Properties.Name) {
    foreach ($entry in @($hooks.$event)) {
        foreach ($hook in @($entry.hooks)) {
            if (-not $hook.args) { throw "$event : entry in forma shell (manca 'args')" }
            # In forma exec Windows non passa dalla shell: il comando deve essere
            # un vero eseguibile, non uno script o una riga da interpretare.
            if ($hook.command -notmatch '\.exe$') { throw "$event : 'command' non e' un .exe: $($hook.command)" }
            if (-not (Test-Path $hook.command)) { throw "$event : comando inesistente: $($hook.command)" }
        }
    }
}

# ---------------------------------------------------------------------
# settings.json: si tocca SOLO la chiave 'hooks'
# ---------------------------------------------------------------------
$json = if (Test-Path $Settings) { Get-Content $Settings -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }

if ($json.PSObject.Properties.Name -contains 'hooks') {
    $existing = $json.hooks | ConvertTo-Json -Depth 20
    $ours = $existing -match 'hooks/dispatch\.py'
    if (-not $ours -and -not $Force) {
        throw "settings.json ha gia' una sezione 'hooks' non riconosciuta. Controllala, poi rilancia con -Force."
    }
    $json.hooks = $hooks
} else {
    $json | Add-Member -NotePropertyName 'hooks' -NotePropertyValue $hooks
}

# ---------------------------------------------------------------------
# Scrittura: UTF-8 SENZA BOM (il BOM di PowerShell 5.1 puo' rompere il parser)
# ---------------------------------------------------------------------
$text = $json | ConvertTo-Json -Depth 20
$check = $text | ConvertFrom-Json        # il risultato deve essere JSON valido
if (-not $check.hooks) { throw "La sezione 'hooks' risultante e' vuota: niente da scrivere." }

if ($DryRun) { $text; return }

if (Test-Path $Settings) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Copy-Item $Settings (Join-Path $ClaudeHome "settings.json.bak-$stamp")
}
[System.IO.File]::WriteAllText($Settings, $text, $Utf8NoBom)
Write-Host "settings.json: sezione 'hooks' aggiornata in forma exec. Riavvia Claude Code." -ForegroundColor Green
Write-Host "Per tornare indietro: .\apply-hooks-v2.ps1 -Revert"
