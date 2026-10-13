# =====================================================================
# superclaude: applica settings.json del profilo scelto (lanciatore sottile).
#
#   apply-settings.ps1 -Profile superclaude            applica (con backup datato)
#   apply-settings.ps1 -Profile deepseek -DryRun       riassunto delle modifiche per chiave, senza scrivere
#   apply-settings.ps1 -Profile deepseek -DryRun -Full mostra anche il JSON risultante completo
#   apply-settings.ps1 -Profile superclaude -Revert    anteprima del ripristino dell'ultimo backup dell'applicatore
#   apply-settings.ps1 -Profile superclaude -Revert -Yes  ripristina (salvando prima il file corrente)
#   apply-settings.ps1 -Profile superclaude -Force     sovrascrive una sezione 'hooks' non riconosciuta
#   apply-settings.ps1 -Profile superclaude -DropStop  omette la registrazione di Stop
#
# Lo esegue il proprietario nel proprio terminale. Compatibile con Windows
# PowerShell 5.1 e PowerShell 7. Tutta la logica sta in
# scripts\settings\apply_settings.py (hook da hooks\hooks.json, regole condivise
# da permission-rules.json, modello del profilo DeepSeek da deepseek.base.json).
# =====================================================================
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('superclaude', 'deepseek')]
    [string]$Profile,
    [switch]$DryRun,
    [switch]$Full,
    [switch]$Revert,
    [switch]$Yes,
    [switch]$Force,
    [switch]$DropStop
)
$ErrorActionPreference = 'Stop'

$ClaudeHome = Join-Path $env:USERPROFILE '.claude'
$Python     = Join-Path $ClaudeHome '.venv\Scripts\python.exe'
$Applier    = Join-Path $ClaudeHome 'scripts\settings\apply_settings.py'
foreach ($need in $Python, $Applier) {
    if (-not (Test-Path $need)) { throw "Prerequisito mancante: $need" }
}

$argv = @($Applier, '--profile', $Profile)
if ($DryRun)   { $argv += '--dry-run' }
if ($Full)     { $argv += '--full' }
if ($Revert)   { $argv += '--revert' }
if ($Yes)      { $argv += '--yes' }
if ($Force)    { $argv += '--force' }
if ($DropStop) { $argv += '--drop-stop' }

& $Python @argv
exit $LASTEXITCODE
