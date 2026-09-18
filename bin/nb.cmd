@echo off
rem Shim `nb` per cmd.exe: da copiare in %USERPROFILE%\.claude\bin oppure da usare da qui.
setlocal
set "NB_SKILL=%~dp0.."
if not exist "%NB_SKILL%\nb_main.py" set "NB_SKILL=%USERPROFILE%\.claude\skills\notebook"
call "%NB_SKILL%\notebook.cmd" %*
exit /b %ERRORLEVEL%
