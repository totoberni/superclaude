# =====================================================================
# Fase 1: rimuove l'hook sonda da ~\.claude\settings.json
# Toglie solo la sezione 'hooks' aggiunta dalla sonda; il resto resta com'è
# (così non si perdono modifiche fatte nel frattempo, es. /model o /effort).
# =====================================================================
$ErrorActionPreference = 'Stop'

$ClaudeHome = Join-Path $env:USERPROFILE '.claude'
$Settings   = Join-Path $ClaudeHome 'settings.json'
$Backup     = Join-Path $ClaudeHome 'settings.json.pre-probe.bak'

if (-not (Test-Path $Settings)) { throw "settings.json non trovato" }

$json = Get-Content $Settings -Raw | ConvertFrom-Json
if ($json.PSObject.Properties.Name -contains 'hooks') {
    $isProbe = ($json.hooks | ConvertTo-Json -Depth 20) -match 'tmp/probe/probe\.py'
    if (-not $isProbe) { throw "La sezione 'hooks' non sembra quella della sonda: non la tocco." }
    $json.PSObject.Properties.Remove('hooks')
    $text = $json | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($Settings, $text, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Sonda rimossa da settings.json" -ForegroundColor Green
} else {
    Write-Host "Nessuna sezione 'hooks' presente: niente da fare."
}

if (Test-Path $Backup) { Remove-Item $Backup -Confirm:$false }
Write-Host "I log della sonda restano in $ClaudeHome\tmp\probe"
