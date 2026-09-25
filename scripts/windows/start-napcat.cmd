@echo off
setlocal
powershell.exe -NoProfile -NonInteractive -Command "if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo ERROR: NapCat requires administrator rights. Start the bot manager as administrator.
  exit /b 1
)

set "NAPCAT_LAUNCHER=%~1"
set "NAPCAT_QQ=%~2"

if not defined NAPCAT_LAUNCHER (
  echo ERROR: NapCat launcher path was not provided.
  exit /b 1
)

if not exist "%NAPCAT_LAUNCHER%" (
  echo ERROR: NapCat launcher not found:
  echo %NAPCAT_LAUNCHER%
  exit /b 1
)

for %%I in ("%NAPCAT_LAUNCHER%") do (
  cd /d "%%~dpI"
  set "NAPCAT_FILE=%%~nxI"
)

echo NapCat working directory: %CD%
if defined NAPCAT_QQ (
  echo Starting NapCat with quick-login QQ: %NAPCAT_QQ%
  call "%NAPCAT_FILE%" -q %NAPCAT_QQ%
) else (
  echo Starting NapCat without quick-login.
  call "%NAPCAT_FILE%"
)

if errorlevel 1 (
  echo.
  echo NapCat launcher returned an error. Keep this window open and check the message above.
  exit /b 1
)

endlocal
