@echo off
setlocal
set "SCRIPT=C:\Users\XXX\.codex\skills\auto-commit\scripts\auto_commit_from_diff.py"

if not exist "%SCRIPT%" (
  echo [auto-commit] Script not found: %SCRIPT%
  exit /b 1
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%SCRIPT%" %*
  exit /b %errorlevel%
)

py -3 "%SCRIPT%" %*
exit /b %errorlevel%
