# /notebook skill: wrapper sottile per PowerShell. La risoluzione del venv di progetto vive in nb_main.py.
$boot = Join-Path $env:USERPROFILE ".claude\.venv\Scripts\python.exe"
if ($env:VIRTUAL_ENV) {
    $cand = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (Test-Path $cand) { $boot = $cand }
}
if (-not (Test-Path $boot)) {
    [Console]::Error.WriteLine("[notebook] no Python interpreter found (expected ~/.claude/.venv)")
    exit 127
}
& $boot (Join-Path $PSScriptRoot "nb_main.py") @args
exit $LASTEXITCODE
