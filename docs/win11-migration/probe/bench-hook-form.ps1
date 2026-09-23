# =====================================================================
# Misura U-1: quanto costa un hook PreToolUse in forma shell e in forma exec.
#
#   .\bench-hook-form.ps1              20 ripetizioni per forma
#   .\bench-hook-form.ps1 -Runs 40     piu' ripetizioni
#
# "Prima" (forma shell): il comando registrato oggi in settings.json passa da
# Git Bash, che lo interpreta e poi lancia python.exe.
# "Dopo" (forma exec): python.exe viene avviato direttamente con la lista di
# argomenti, senza shell.
#
# Il payload e' un PreToolUse realistico su Bash, cioe' il caso piu' frequente;
# gli handler eseguiti sono gli stessi nelle due forme, quindi la differenza
# misura solo il livello di shell.
#
# Gli handler girano davvero: nessuno di loro modifica file dell'utente con
# questo payload, ma la sessione fittizia 'bench-u1' lascia i marcatori one-shot
# in ~\.claude\state\bench-u1\. Lo script cancella quella cartella alla fine.
# =====================================================================
[CmdletBinding()]
param(
    [int]$Runs = 20,
    [int]$Warmup = 3
)
$ErrorActionPreference = 'Stop'

$ClaudeHome = Join-Path $env:USERPROFILE '.claude'
$Python     = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
$Dispatch   = Join-Path $ClaudeHome 'hooks\dispatch.py'
foreach ($need in $Python, $Dispatch) {
    if (-not (Test-Path $need)) { throw "Prerequisito mancante: $need" }
}
$bash = $null
$cands = @(
    $env:CLAUDE_CODE_GIT_BASH_PATH,
    (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe'),
    'C:\Program Files\Git\bin\bash.exe'
) + @((Get-Command bash.exe -ErrorAction SilentlyContinue).Source)
foreach ($c in $cands) { if ($c -and (Test-Path $c)) { $bash = $c; break } }
if (-not $bash) { throw "bash.exe non trovato: impossibile misurare la forma shell." }

function ConvertTo-Posix([string]$p) { $p -replace '\\', '/' }
$pyPosix  = ConvertTo-Posix $Python
$dspPosix = ConvertTo-Posix $Dispatch
$shellCmd = "'$pyPosix' -S '$dspPosix' PreToolUse"

$payload = [ordered]@{
    hook_event_name = 'PreToolUse'
    session_id      = 'bench-u1'
    cwd             = (Join-Path $env:USERPROFILE 'code')
    tool_name       = 'Bash'
    tool_input      = [ordered]@{ command = 'echo hook-bench'; description = 'benchmark U-1' }
} | ConvertTo-Json -Depth 5 -Compress

function Measure-Form([string]$label, [scriptblock]$invoke) {
    for ($i = 0; $i -lt $Warmup; $i++) { & $invoke | Out-Null }
    $samples = New-Object System.Collections.Generic.List[double]
    for ($i = 0; $i -lt $Runs; $i++) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        & $invoke | Out-Null
        $sw.Stop()
        $samples.Add($sw.Elapsed.TotalMilliseconds)
    }
    $sorted = $samples | Sort-Object
    [pscustomobject]@{
        Forma    = $label
        MedianMs = [math]::Round($sorted[[int]($sorted.Count / 2)], 1)
        MeanMs   = [math]::Round(($samples | Measure-Object -Average).Average, 1)
        MinMs    = [math]::Round($sorted[0], 1)
        MaxMs    = [math]::Round($sorted[-1], 1)
    }
}

$shell = Measure-Form 'shell (bash -c)' { $payload | & $bash -c $shellCmd 2>$null }
$exec  = Measure-Form 'exec (args)'     { $payload | & $Python -S $Dispatch PreToolUse 2>$null }

# Pulizia dello stato lasciato dalla sessione fittizia
$benchState = Join-Path $ClaudeHome 'state\bench-u1'
if (Test-Path $benchState) { Remove-Item $benchState -Recurse -Force -Confirm:$false }

$shell, $exec | Format-Table -AutoSize
$delta = $shell.MedianMs - $exec.MedianMs
Write-Host ("Differenza mediana: {0} ms per invocazione PreToolUse ({1} ripetizioni)." -f [math]::Round($delta, 1), $Runs)
