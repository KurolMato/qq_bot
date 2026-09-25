@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
chcp 65001 >NUL

echo ============================================================
echo   Video Analysis Bot - install laptop prerequisites
echo ============================================================
echo This will install Python 3.12, Node.js LTS, Git and FFmpeg with winget.
echo Windows may ask for administrator approval.
echo.

where winget.exe >NUL 2>NUL
if errorlevel 1 goto :missing_winget

echo [1/4] Installing Python 3.12...
winget install --id Python.Python.3.12 -e --source winget --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto :failed

echo.
echo [2/4] Installing Node.js LTS...
winget install --id OpenJS.NodeJS.LTS -e --source winget --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto :failed

echo.
echo [3/4] Installing Git...
winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto :failed

echo.
echo [4/4] Installing FFmpeg...
winget install --id Gyan.FFmpeg -e --source winget --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto :failed

echo.
echo Prerequisites installed successfully.
echo Close this window, then open 机器人管理.cmd and choose dependency setup.
echo If a command is still not found, restart Windows once.
pause
exit /b 0

:missing_winget
echo ERROR: winget was not found.
echo Open Microsoft Store, install or update "App Installer", then run this file again.
pause
exit /b 1

:failed
echo.
echo ERROR: One prerequisite was not installed.
echo Read the error above. You can run this file again; installed packages will not be duplicated.
pause
exit /b 1
