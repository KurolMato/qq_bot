@echo off
setlocal
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
chcp 65001 >NUL
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
call "%PROJECT_ROOT%\tools\select_python.cmd"

if not exist "%BOT_PYTHON%" (
  echo ERROR: Python environment not found.
  exit /b 1
)

"%BOT_PYTHON%" -m qq_bot.restart_bot
exit /b %ERRORLEVEL%
