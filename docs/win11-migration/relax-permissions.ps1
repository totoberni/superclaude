# =====================================================================
# superclaude / Windows 11: toglie i freni sui permessi da ~\.claude\settings.json
#
#   .\relax-permissions.ps1               rimuove permissions.deny e permissions.ask
#   .\relax-permissions.ps1 -BroadAllow   in più: consente gli strumenti per intero
#                                         (Bash, PowerShell, Write, Edit, WebFetch...)
#   .\relax-permissions.ps1 -DryRun       mostra la sezione risultante senza scrivere
#   .\relax-permissions.ps1 -Revert       ripristina l'ultimo backup
#
# Scelta del proprietario: account personale, spazio utente limitato, nessuna
# sandbox da imitare. Le garanzie di qualità NON passano da qui: restano attive
# le guardie degli hook (write-acl, git-policy, commit-gate, notebook-guard...).
# Compatibile con Windows PowerShell 5.1 e PowerShell 7. Idempotente.
# =====================================================================
[CmdletBinding()]
param(
    [switch]$BroadAllow,
    [switch]$DryRun,
    [switch]$Revert
)
$ErrorActionPreference = 'Stop'

$ClaudeHome = Join-Path $env:USERPROFILE '.claude'
$Settings   = Join-Path $ClaudeHome 'settings.json'
$Utf8NoBom  = New-Object System.Text.UTF8Encoding($false)

if ($Revert) {
    $last = Get-ChildItem $ClaudeHome -Filter 'settings.json.bak-*' | Sort-Object Name | Select-Object -Last 1
    if (-not $last) { throw "Nessun backup trovato in $ClaudeHome" }
    Copy-Item $last.FullName $Settings -Force
    Write-Host "Ripristinato: $($last.Name)" -ForegroundColor Green
    return
}

if (-not (Test-Path $Settings)) { throw "settings.json non trovato: $Settings" }
$json = Get-Content $Settings -Raw | ConvertFrom-Json

if (-not ($json.PSObject.Properties.Name -contains 'permissions')) {
    $json | Add-Member -NotePropertyName permissions -NotePropertyValue ([pscustomobject]@{})
}
$perm = $json.permissions

$removed = @()
foreach ($name in 'deny', 'ask') {
    if ($perm.PSObject.Properties.Name -contains $name) {
        $removed += "{0} ({1} regole)" -f $name, @($perm.$name).Count
        $perm.PSObject.Properties.Remove($name)
    }
}

if ($BroadAllow) {
    # Una regola senza parentesi consente lo strumento per intero: nessuna richiesta
    # di conferma e nessun passaggio dal classificatore della modalità auto.
    $broad = @('Read', 'Glob', 'Grep', 'Write', 'Edit', 'Bash', 'PowerShell',
               'WebFetch', 'WebSearch', 'Skill', 'Agent')
    $current = @()
    if ($perm.PSObject.Properties.Name -contains 'allow') { $current = @($perm.allow) }
    # Le regole specifiche (es. "Bash(git status:*)") diventano ridondanti: si tolgono
    $kept   = @($current | Where-Object { $_ -and ($_ -notmatch '^(Bash|PowerShell)\(') })
    $merged = @($broad + $kept | Select-Object -Unique)
    if ($perm.PSObject.Properties.Name -contains 'allow') { $perm.allow = $merged }
    else { $perm | Add-Member -NotePropertyName allow -NotePropertyValue $merged }
}

$text = $json | ConvertTo-Json -Depth 20
$null = $text | ConvertFrom-Json   # deve restare JSON valido

if ($DryRun) {
    "Rimosso: $(if ($removed) { $removed -join ', ' } else { 'niente (gia'' assenti)' })"
    $json.permissions | ConvertTo-Json -Depth 5
    return
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item $Settings (Join-Path $ClaudeHome "settings.json.bak-$stamp")
# UTF-8 SENZA BOM: il BOM di PowerShell 5.1 può rompere il parser JSON
[System.IO.File]::WriteAllText($Settings, $text, $Utf8NoBom)

Write-Host "Rimosso: $(if ($removed) { $removed -join ', ' } else { 'niente (gia'' assenti)' })" -ForegroundColor Green
if ($BroadAllow) { Write-Host "Consentiti per intero: Bash, PowerShell, Write, Edit, WebFetch, WebSearch, Skill, Agent" -ForegroundColor Green }
Write-Host "Riavvia Claude Code (o apri /permissions) per applicare. Per tornare indietro: .\relax-permissions.ps1 -Revert"
