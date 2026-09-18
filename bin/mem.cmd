@echo off
rem Shim cmd.exe: tutta la logica vive in sc_shim.py
"%USERPROFILE%\.claude\.venv\Scripts\python.exe" "%USERPROFILE%\.claude\bin\sc_shim.py" mem %*
