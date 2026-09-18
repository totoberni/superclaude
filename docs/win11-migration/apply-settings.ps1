# =====================================================================
# superclaude / Windows 11: applica la configurazione a ~\.claude\settings.json
#
#   .\apply-settings.ps1            applica (con backup datato)
#   .\apply-settings.ps1 -DryRun    mostra il JSON risultante senza scrivere
#   .\apply-settings.ps1 -Revert    ripristina l'ultimo backup
#   .\apply-settings.ps1 -Force     sovrascrive una sezione 'hooks' non riconosciuta
#
# Compatibile con Windows PowerShell 5.1 e PowerShell 7. Idempotente.
# Tocca SOLO: hooks, env, statusLine, permissions (unione), sandbox (rimosso).
# Non tocca: model, modelSettings, tui, theme, enabledPlugins e tutto il resto.
# =====================================================================
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Revert,
    [switch]$Force,
    [switch]$NoPermissions,   # non modificare la sezione permissions
    [switch]$NoStatusLine     # non impostare la statusline
)
$ErrorActionPreference = 'Stop'

$ClaudeHome = Join-Path $env:USERPROFILE '.claude'
$Settings   = Join-Path $ClaudeHome 'settings.json'
$BasePython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
$VenvPython = Join-Path $ClaudeHome '.venv\Scripts\python.exe'
$Dispatch   = Join-Path $ClaudeHome 'hooks\dispatch.py'
$StatusPy   = Join-Path $ClaudeHome 'scripts\statusline_telemetry.py'
$Utf8NoBom  = New-Object System.Text.UTF8Encoding($false)

function ConvertTo-Posix([string]$p) { $p -replace '\\', '/' }

# ---------------------------------------------------------------------
# Ripristino
# ---------------------------------------------------------------------
if ($Revert) {
    $last = Get-ChildItem $ClaudeHome -Filter 'settings.json.bak-*' | Sort-Object Name | Select-Object -Last 1
    if (-not $last) { throw "Nessun backup trovato in $ClaudeHome" }
    Copy-Item $last.FullName $Settings -Force
    Write-Host "Ripristinato: $($last.Name)" -ForegroundColor Green
    return
}

# ---------------------------------------------------------------------
# Controlli preliminari
# ---------------------------------------------------------------------
foreach ($need in $BasePython, $VenvPython, $Dispatch) {
    if (-not (Test-Path $need)) { throw "Prerequisito mancante: $need" }
}

$json = if (Test-Path $Settings) { Get-Content $Settings -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }

function Set-Prop($obj, [string]$name, $value) {
    if ($obj.PSObject.Properties.Name -contains $name) { $obj.$name = $value }
    else { $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value }
}

# ---------------------------------------------------------------------
# hooks: un solo dispatcher Python per evento.
# Gli hook girano in Git Bash: percorsi con slash in avanti, tra virgolette.
# Interprete di base con -S (niente site-packages): avvio più rapido.
# I matcher di Pre/PostToolUse sono limitati agli strumenti che le guardie
# ispezionano davvero: ogni invocazione costa circa 200 ms su questa macchina.
# ---------------------------------------------------------------------
if ($json.PSObject.Properties.Name -contains 'hooks') {
    $existing = $json.hooks | ConvertTo-Json -Depth 20
    $ours = ($existing -match 'hooks/dispatch\.py') -or ($existing -match 'tmp/probe/probe\.py')
    if (-not $ours -and -not $Force) {
        throw "settings.json ha già una sezione 'hooks' non riconosciuta. Controllala, poi rilancia con -Force."
    }
}

$py  = ConvertTo-Posix $BasePython
$dsp = ConvertTo-Posix $Dispatch
function New-HookEntry([string]$event, [string]$matcher, [int]$timeout) {
    [ordered]@{
        matcher = $matcher
        hooks   = @([ordered]@{
            type    = 'command'
            command = ('"{0}" -S "{1}" {2}' -f $py, $dsp, $event)
            timeout = $timeout
        })
    }
}

$hooks = [ordered]@{
    SessionStart  = @(New-HookEntry 'SessionStart'  '' 15)
    SubagentStart = @(New-HookEntry 'SubagentStart' '' 10)
    PreToolUse    = @(New-HookEntry 'PreToolUse'  'Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|Agent' 10)
    PostToolUse   = @(New-HookEntry 'PostToolUse' 'Agent|Write|Edit|MultiEdit' 10)
    SubagentStop  = @(New-HookEntry 'SubagentStop'  '' 10)
    PreCompact    = @(New-HookEntry 'PreCompact'    '' 20)
    Stop          = @(New-HookEntry 'Stop'          '' 10)
    SessionEnd    = @(New-HookEntry 'SessionEnd'    '' 30)
}
Set-Prop $json 'hooks' ([pscustomobject]$hooks)

# ---------------------------------------------------------------------
# env: sostituisce superclaude.env e il wrapper bash `claude()`
# ---------------------------------------------------------------------
$memGuidelines = 'Your persistent memory is a hybrid-search SQLite DB at ~/.claude/agent-memory/.memory.db (NOT Markdown files; there is no MEMORY.md to read). To recall, run: mem search "<query>" -k 8, then mem get <slug> for the full body. Query the DB proactively whenever a task touches a project, library, tool, or past decision. Memory is tiered (instance/<agent>, shared-projects, shared-global, class) and typed (feedback/project/reference/user). To write or update memory use the memory skills (/remember, /good-idea, /lt-mem, /mistake); never hand-write .md memory files.'

$envWanted = [ordered]@{
    HF_HUB_OFFLINE                   = '1'
    TRANSFORMERS_OFFLINE             = '1'
    HF_HUB_DISABLE_SYMLINKS_WARNING  = '1'
    PYTHONUTF8                       = '1'
    CLAUDE_CODE_DISABLE_AUTO_MEMORY  = '1'     # la memoria è il DB, non i file .md integrati
    CLAUDE_COWORK_MEMORY_GUIDELINES  = $memGuidelines
}
if (-not ($json.PSObject.Properties.Name -contains 'env')) { Set-Prop $json 'env' ([pscustomobject]@{}) }
foreach ($k in $envWanted.Keys) { Set-Prop $json.env $k $envWanted[$k] }

# ---------------------------------------------------------------------
# statusLine
# ---------------------------------------------------------------------
if (-not $NoStatusLine) {
    if (Test-Path $StatusPy) {
        Set-Prop $json 'statusLine' ([pscustomobject][ordered]@{
            type    = 'command'
            command = ('"{0}" "{1}"' -f (ConvertTo-Posix $VenvPython), (ConvertTo-Posix $StatusPy))
        })
    } else {
        Write-Warning "statusline_telemetry.py non trovato: statusLine non impostata."
    }
}

# ---------------------------------------------------------------------
# permissions: UNIONE con quelle esistenti (mai rimozioni).
# Su Windows nativo non esiste la sandbox: l'elenco 'allow' è volutamente più
# stretto di quello WSL. Comandi distruttivi o di rete (rm, curl, kill, find
# -delete, Remove-Item, Invoke-WebRequest...) restano soggetti a conferma.
# ---------------------------------------------------------------------
if (-not $NoPermissions) {
    $allow = @(
        'Read', 'Glob', 'Grep', 'Write', 'Edit',
        'Bash(mem:*)', 'Bash(scpy:*)', 'PowerShell(mem:*)', 'PowerShell(scpy:*)',
        'Bash(git status:*)', 'Bash(git diff:*)', 'Bash(git log:*)', 'Bash(git show:*)',
        'Bash(git branch --show-current)', 'Bash(git rev-parse:*)', 'Bash(git ls-files:*)',
        'Bash(ls:*)', 'Bash(cat:*)', 'Bash(head:*)', 'Bash(tail:*)', 'Bash(wc:*)',
        'Bash(diff:*)', 'Bash(file:*)', 'Bash(date:*)', 'Bash(tree:*)',
        'PowerShell(Get-ChildItem:*)', 'PowerShell(Get-Content:*)', 'PowerShell(Test-Path:*)',
        'PowerShell(Get-Command:*)', 'PowerShell(Get-Process:*)'
    )
    $deny = @(
        'Bash(git branch -D *)', 'Bash(git checkout -- .)', 'Bash(git clean -f*)',
        'Bash(git reset --hard*)', 'Bash(git push --force*)', 'Bash(git push -f*)',
        'Bash(rm -rf /)', 'Bash(rm -rf ~)', 'Bash(rm -rf ~/)', 'Bash(rm -rf ~/*)',
        'Bash(rm -fr ~)', 'Bash(rm -fr ~/)', 'Bash(find ~ -delete)',
        'PowerShell(git branch -D *)', 'PowerShell(git checkout -- .)', 'PowerShell(git clean -f*)',
        'PowerShell(git reset --hard*)', 'PowerShell(git push --force*)', 'PowerShell(git push -f*)',
        'PowerShell(Remove-Item -Recurse -Force ~*)', 'PowerShell(Remove-Item -Recurse -Force $HOME*)',
        'PowerShell(Remove-Item -Recurse -Force $env:USERPROFILE*)', 'PowerShell(Remove-Item -Recurse -Force C:\*)',
        'PowerShell(Format-Volume*)', 'PowerShell(Clear-Disk*)'
    )
    if (-not ($json.PSObject.Properties.Name -contains 'permissions')) { Set-Prop $json 'permissions' ([pscustomobject]@{}) }
    foreach ($pair in @(@('allow', $allow), @('deny', $deny))) {
        $name, $wanted = $pair[0], $pair[1]
        $current = @()
        if ($json.permissions.PSObject.Properties.Name -contains $name) { $current = @($json.permissions.$name) }
        $merged = @($current + $wanted | Where-Object { $_ } | Select-Object -Unique)
        Set-Prop $json.permissions $name $merged
    }
}

# ---------------------------------------------------------------------
# sandbox: non supportata su Windows nativo, il blocco sarebbe lettera morta
# ---------------------------------------------------------------------
if ($json.PSObject.Properties.Name -contains 'sandbox') { $json.PSObject.Properties.Remove('sandbox') }

# ---------------------------------------------------------------------
# Scrittura: UTF-8 SENZA BOM (il BOM di PowerShell 5.1 può rompere il parser)
# ---------------------------------------------------------------------
$text = $json | ConvertTo-Json -Depth 20
$null = $text | ConvertFrom-Json   # il risultato deve essere JSON valido

if ($DryRun) { $text; return }

if (Test-Path $Settings) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Copy-Item $Settings (Join-Path $ClaudeHome "settings.json.bak-$stamp")
}
[System.IO.File]::WriteAllText($Settings, $text, $Utf8NoBom)
Write-Host "settings.json aggiornato. Riavvia Claude Code per caricare gli hook." -ForegroundColor Green
Write-Host "Per tornare indietro: .\apply-settings.ps1 -Revert"
