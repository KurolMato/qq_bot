@echo off
setlocal
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
powershell.exe -NoProfile -NonInteractive -Command "if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo ERROR: Run the bot manager as administrator so NapCat automatic recovery can work.
  exit /b 1
)
call "%PROJECT_ROOT%\tools\select_python.cmd"

set "NAPCAT_LAUNCHER=%PROJECT_ROOT%\..\NapCat.Shell\launcher-win10.bat"
set "NAPCAT_QQ="
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /i "%%A"=="NAPCAT_LAUNCHER" set "NAPCAT_LAUNCHER=%%B"
    if /i "%%A"=="NAPCAT_QQ" set "NAPCAT_QQ=%%B"
  )
)

if not exist "%NAPCAT_LAUNCHER%" if exist "%PROJECT_ROOT%\NapCat.Shell\launcher-win10.bat" (
  set "NAPCAT_LAUNCHER=%PROJECT_ROOT%\NapCat.Shell\launcher-win10.bat"
  echo Using NapCat launcher inside the project folder.
)

if not exist "%NAPCAT_LAUNCHER%" if exist "%PROJECT_ROOT%\..\NapCat.Shell\launcher-win10.bat" (
  set "NAPCAT_LAUNCHER=%PROJECT_ROOT%\..\NapCat.Shell\launcher-win10.bat"
  echo Using portable NapCat launcher beside the project folder.
)

if not exist "%BOT_PYTHON%" (
  echo ERROR: Python environment for this computer was not found.
  echo Open 机器人管理.cmd and choose dependency setup once on this computer.
  pause
  exit /b 1
)

tasklist /FI "IMAGENAME eq NapCatWinBootMain.exe" 2>NUL | find /I "NapCatWinBootMain.exe" >NUL
if errorlevel 1 (
  if exist "%NAPCAT_LAUNCHER%" (
    if defined NAPCAT_QQ (
      start "" /b "%BOT_PYTHONW%" -m qq_bot.hidden_launcher --log "data\napcat-launch.log" -- "%ComSpec%" /d /c call "%PROJECT_ROOT%\scripts\windows\start-napcat.cmd" "%NAPCAT_LAUNCHER%" "%NAPCAT_QQ%"
      echo Starting NapCat with quick-login QQ: %NAPCAT_QQ%
    ) else (
      start "" /b "%BOT_PYTHONW%" -m qq_bot.hidden_launcher --log "data\napcat-launch.log" -- "%ComSpec%" /d /c call "%PROJECT_ROOT%\scripts\windows\start-napcat.cmd" "%NAPCAT_LAUNCHER%"
      echo Starting NapCat without a quick-login account.
    )
    echo Approve the Windows administrator prompt if it appears.
  ) else (
    echo WARNING: NapCat launcher not found: %NAPCAT_LAUNCHER%
    echo Update NAPCAT_LAUNCHER in .env.
  )
) else (
  echo NapCat is already running; skipped duplicate start.
)

netstat -ano | findstr ":8000" | findstr "LISTENING" >NUL
if errorlevel 1 (
  start "" /b "%BOT_PYTHONW%" -m qq_bot.hidden_launcher --log "data\video-api.log" -- "%BOT_PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
) else (
  echo Port 8000 is already listening; skipped duplicate API start.
)

netstat -ano | findstr ":8081" | findstr "LISTENING" >NUL
if errorlevel 1 (
  start "Video QQ Bot" cmd /k call "%PROJECT_ROOT%\scripts\windows\start-qq-bot.cmd"
) else (
  echo Port 8081 is already listening; skipped duplicate QQ Bot start.
)

if exist "%BOT_PYTHONW%" if exist "%NAPCAT_LAUNCHER%" (
  start "" /b "%BOT_PYTHONW%" -m qq_bot.napcat_watchdog --launcher "%NAPCAT_LAUNCHER%" --qq "%NAPCAT_QQ%"
  echo NapCat connection watchdog enabled.
)

call :wait_api
call :wait_bot
echo Background services are hidden. Keep the Video QQ Bot window open.
endlocal
exit /b 0

:wait_api
for /L %%I in (1,1,20) do (
  curl.exe -fsS --max-time 2 "http://127.0.0.1:8000/health" >NUL 2>NUL
  if not errorlevel 1 (
    echo Web API ready: http://127.0.0.1:8000
    exit /b 0
  )
  timeout /t 1 /nobreak >NUL
)
echo WARNING: Web API did not become healthy within 20 seconds. Check the Video MP4 Web API window.
exit /b 1

:wait_bot
for /L %%I in (1,1,20) do (
  netstat -ano | findstr ":8081" | findstr "LISTENING" >NUL
  if not errorlevel 1 (
    echo QQ Bot ready: ws://127.0.0.1:8081/onebot/v11/ws
    exit /b 0
  )
  timeout /t 1 /nobreak >NUL
)
echo WARNING: QQ Bot did not listen on port 8081 within 20 seconds. Check the Video QQ Bot window.
exit /b 1
