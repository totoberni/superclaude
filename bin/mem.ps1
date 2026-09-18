# Shim PowerShell: tutta la logica vive in sc_shim.py
& "$env:USERPROFILE\.claude\.venv\Scripts\python.exe" "$env:USERPROFILE\.claude\bin\sc_shim.py" mem @args
exit $LASTEXITCODE
