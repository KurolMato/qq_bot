@echo off
setlocal EnableExtensions EnableDelayedExpansion
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\tools\select_python.cmd"

if not exist "%BOT_PYTHON%" (
  echo ERROR: Python environment not found. Open 机器人管理.cmd and choose dependency setup first.
  pause
  exit /b 1
)

netstat -ano | findstr ":8000" | findstr "LISTENING" >NUL
if errorlevel 1 (
  start "Video MP4 Web API" cmd /k ""%BOT_PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log"
)

for /L %%I in (1,1,25) do (
  if exist "secrets\admin-token.txt" (
    curl.exe -fsS --max-time 2 "http://127.0.0.1:8000/health" >NUL 2>NUL
    if not errorlevel 1 goto :open
  )
  timeout /t 1 /nobreak >NUL
)

echo ERROR: Management service did not start. Check the Video MP4 Web API window.
pause
exit /b 1

:open
set "ADMIN_TOKEN="
set /p ADMIN_TOKEN=<"secrets\admin-token.txt"
start "" "http://127.0.0.1:8000/admin?token=!ADMIN_TOKEN!"
exit /b 0
