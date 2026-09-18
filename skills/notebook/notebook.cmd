@echo off
rem /notebook skill: wrapper sottile per cmd.exe. La risoluzione del venv di progetto vive in nb_main.py.
setlocal
set "NB_BOOT=%USERPROFILE%\.claude\.venv\Scripts\python.exe"
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" set "NB_BOOT=%VIRTUAL_ENV%\Scripts\python.exe"
if not exist "%NB_BOOT%" (
  echo [notebook] no Python interpreter found ^(expected %%USERPROFILE%%\.claude\.venv^) 1>&2
  exit /b 127
)
"%NB_BOOT%" "%~dp0nb_main.py" %*
exit /b %ERRORLEVEL%
