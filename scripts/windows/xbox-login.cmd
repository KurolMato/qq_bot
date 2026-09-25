@echo off
setlocal
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\tools\select_python.cmd"

if not exist "%BOT_PYTHON%" (
  echo Python environment not found. Use 机器人管理.cmd to install dependencies first.
  pause
  exit /b 1
)

"%BOT_PYTHON%" -m qq_bot.xbox_login
set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%
