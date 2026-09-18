# Shim `nb` per PowerShell: da copiare in ~/.claude/bin oppure da usare da qui.
$skill = Join-Path $PSScriptRoot ".."
if (-not (Test-Path (Join-Path $skill "nb_main.py"))) { $skill = Join-Path $env:USERPROFILE ".claude\skills\notebook" }
& (Join-Path $skill "notebook.ps1") @args
exit $LASTEXITCODE
