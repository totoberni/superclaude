# =====================================================================
# Fase 1: installa l'hook sonda in ~\.claude\settings.json
# Compatibile con Windows PowerShell 5.1 e PowerShell 7. Idempotente.
# Per rimuoverlo: .\probe-remove.ps1
# =====================================================================
$ErrorActionPreference = 'Stop'

$ClaudeHome = Join-Path $env:USERPROFILE '.claude'
$Settings   = Join-Path $ClaudeHome 'settings.json'
$Backup     = Join-Path $ClaudeHome 'settings.json.pre-probe.bak'
$ProbeDir   = Join-Path $ClaudeHome 'tmp\probe'
$ProbeSrc   = Join-Path $PSScriptRoot 'probe.py'
$ProbeDst   = Join-Path $ProbeDir 'probe.py'
$Python     = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'

if (-not (Test-Path $Python))   { throw "Python non trovato: $Python" }
if (-not (Test-Path $ProbeSrc)) { throw "probe.py non trovato accanto a questo script" }

New-Item -ItemType Directory -Force $ProbeDir | Out-Null
Copy-Item $ProbeSrc $ProbeDst -Force

# Backup una sola volta: non sovrascrivere un backup già esistente
if ((Test-Path $Settings) -and -not (Test-Path $Backup)) {
    Copy-Item $Settings $Backup
}

$json = if (Test-Path $Settings) { Get-Content $Settings -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }

# Gli hook girano in Git Bash: percorsi con slash in avanti e tra virgolette
$cmd = '"{0}" "{1}"' -f ($Python -replace '\\', '/'), ($ProbeDst -replace '\\', '/')

$hooks = [ordered]@{}
foreach ($evt in 'SessionStart', 'PreToolUse', 'PostToolUse', 'SubagentStart', 'SubagentStop') {
    $hooks[$evt] = @(
        [ordered]@{
            matcher = ''
            hooks   = @([ordered]@{ type = 'command'; command = $cmd; timeout = 10 })
        }
    )
}

if ($json.PSObject.Properties.Name -contains 'hooks') {
    throw "settings.json contiene già una sezione 'hooks': la sonda non la sovrascrive. Rimuovila o usa probe-remove.ps1."
}
$json | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]$hooks)

# UTF-8 senza BOM: il BOM di PowerShell 5.1 può rompere il parsing JSON
$text = $json | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($Settings, $text, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Sonda installata. Backup: $Backup" -ForegroundColor Green
Write-Host "Riavvia Claude Code; i log finiscono in $ProbeDir"
