@echo off
setlocal
cd /d "%~dp0.."
call "%~dp0select_python.cmd"
if errorlevel 1 (
  echo Python environment not found. Install the project dependencies first.
  pause
  exit /b 1
)
"%BOT_PYTHON%" "%~dp0diagnose_steam_avatar.py" %*
set "TASK_EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %TASK_EXIT_CODE%
