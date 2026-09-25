@echo off
setlocal EnableExtensions EnableDelayedExpansion
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
chcp 65001 >NUL
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ============================================================
echo   Video Analysis Bot - laptop migration setup
echo ============================================================
echo Project: %CD%
echo.

set "PYTHON_CMD="
where py.exe >NUL 2>NUL
if not errorlevel 1 set "PYTHON_CMD=py -3.12"
if not defined PYTHON_CMD (
  where python.exe >NUL 2>NUL
  if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD goto :missing_python

set "VENV_DIR=.venvs\%COMPUTERNAME%"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

if exist "%VENV_PYTHON%" (
  "%VENV_PYTHON%" -c "import sys; print(sys.version)" >NUL 2>NUL
  if errorlevel 1 (
    for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "BACKUP_TIME=%%T"
    echo This computer's saved environment is no longer usable.
    echo Keeping it as %COMPUTERNAME%-old-!BACKUP_TIME! ...
    ren "%VENV_DIR%" "%COMPUTERNAME%-old-!BACKUP_TIME!"
  )
)

if not exist "%VENV_PYTHON%" (
  if not exist ".venvs" mkdir ".venvs"
  echo Creating a Python environment for computer %COMPUTERNAME%...
  %PYTHON_CMD% -m venv "%VENV_DIR%"
  if errorlevel 1 goto :failed
)

echo Installing Python dependencies...
where git.exe >NUL 2>NUL
if errorlevel 1 (
  echo ERROR: Git is missing. Open 机器人管理.cmd and choose Windows prerequisite installation first.
  pause
  exit /b 1
)
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :failed
"%VENV_PYTHON%" -m pip install -r requirements-bot.txt
if errorlevel 1 goto :failed

set "F2_VENV_DIR=.venvs\f2-%COMPUTERNAME%"
set "F2_PYTHON=%F2_VENV_DIR%\Scripts\python.exe"
if not exist "%F2_PYTHON%" (
  echo Creating an isolated environment for Douyin image posts...
  %PYTHON_CMD% -m venv "%F2_VENV_DIR%"
  if errorlevel 1 goto :failed
)
"%F2_PYTHON%" -m pip install -r requirements-douyin-note.txt
if errorlevel 1 goto :failed

where node.exe >NUL 2>NUL
if errorlevel 1 (
  echo.
  echo WARNING: Node.js is missing. Install the current Node.js LTS, then run this script again.
) else (
  set "NXAPI_FOUND="
  if exist "%LOCALAPPDATA%\Programs\nxapi-app\resources\app\dist\bundle\cli-bundle.js" set "NXAPI_FOUND=1"
  if exist "%APPDATA%\npm\node_modules\@samuel\nxapi\bin\nxapi.js" set "NXAPI_FOUND=1"
  if exist "%APPDATA%\npm\node_modules\nxapi\bin\nxapi.js" set "NXAPI_FOUND=1"
  if not defined NXAPI_FOUND (
    echo.
    set /p INSTALL_NXAPI="nxapi is missing. Install nxapi@next now? [Y/N]: "
    if /I "!INSTALL_NXAPI!"=="Y" npm.cmd install --global nxapi@next
  )
)

where ffmpeg.exe >NUL 2>NUL
if errorlevel 1 (
  echo.
  echo WARNING: FFmpeg is missing. Video merging may fail.
  echo Recommended command: winget install --id Gyan.FFmpeg -e
)

if not exist ".env" copy /Y ".env.example" ".env" >NUL
if not exist "data" mkdir "data"
if not exist "secrets" mkdir "secrets"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\tools\harden_secrets.ps1"
if errorlevel 1 (
  echo WARNING: Could not restrict the secrets directory permissions. Try running 机器人管理.cmd as administrator.
)

echo.
echo Setup finished. Opening the local management dashboard...
call "%PROJECT_ROOT%\scripts\windows\open-admin.cmd"
exit /b 0

:missing_python
echo ERROR: Python 3.12 was not found.
echo Install Python 3.12 x64 and enable "Add Python to PATH", then run this script again.
pause
exit /b 1

:failed
echo.
echo ERROR: Setup did not complete. Keep the window open and check the error above.
pause
exit /b 1
