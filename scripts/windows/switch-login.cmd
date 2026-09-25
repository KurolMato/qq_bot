@echo off
setlocal EnableExtensions EnableDelayedExpansion
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
chcp 65001 >nul

set "NXAPI_JS="
if exist "%LOCALAPPDATA%\Programs\nxapi-app\resources\app\dist\bundle\cli-bundle.js" set "NXAPI_JS=%LOCALAPPDATA%\Programs\nxapi-app\resources\app\dist\bundle\cli-bundle.js"
if exist "%APPDATA%\npm\node_modules\@samuel\nxapi\bin\nxapi.js" set "NXAPI_JS=%APPDATA%\npm\node_modules\@samuel\nxapi\bin\nxapi.js"
if exist "%APPDATA%\npm\node_modules\nxapi\bin\nxapi.js" set "NXAPI_JS=%APPDATA%\npm\node_modules\nxapi\bin\nxapi.js"
if not defined NXAPI_JS (
  echo nxapi is not installed.
  echo Run: npm install --global nxapi@next
  pause
  exit /b 1
)

set "NXAPI_SKIP_UPDATE_CHECK=1"
set "NXAPI_USER_AGENT=video-analysis-switch-monitor/0.1.0 (contact: project maintainers)"
set "NODE_OPTIONS=--use-system-ca"

echo This signs the observer Nintendo Account into nxapi.
echo Follow the browser instructions. Never paste the login callback into QQ groups.
echo.
echo Using:
node "%NXAPI_JS%" --version
if errorlevel 1 goto :failed
echo.

rem Query through nxapi so the server receives this client's compatibility headers.
(
  echo Checking whether nxapi currently allows Nintendo Switch Online login...
  set "NXAPI_CONFIG_FILE=%TEMP%\video-analysis-nxapi-config-!RANDOM!-!RANDOM!.json"
  node "%NXAPI_JS%" util remote-config --json > "!NXAPI_CONFIG_FILE!"
  if errorlevel 1 (
    if exist "!NXAPI_CONFIG_FILE!" del /q "!NXAPI_CONFIG_FILE!" >nul 2>nul
    goto :config_unavailable
  )
  powershell.exe -NoProfile -NonInteractive -Command "try { $config = Get-Content -Raw -LiteralPath '!NXAPI_CONFIG_FILE!' | ConvertFrom-Json; if ($null -eq $config.coral) { exit 2 }; exit 0 } catch { exit 1 }"
  set "NXAPI_CONFIG_RESULT=!ERRORLEVEL!"
  del /q "!NXAPI_CONFIG_FILE!" >nul 2>nul
  if "!NXAPI_CONFIG_RESULT!"=="2" goto :coral_disabled
  if not "!NXAPI_CONFIG_RESULT!"=="0" goto :config_unavailable
  echo Nintendo Switch Online login is enabled by nxapi.
  echo.
)

where curl.exe >nul 2>nul
if not errorlevel 1 (
  echo Checking nxapi authentication service...
  set "NXAPI_SERVICE_STATUS="
  for /f %%H in ('curl.exe -sS -o NUL -w "%%{http_code}" --max-time 20 "https://nxapi-znca-api.fancy.org.uk/.well-known/oauth-protected-resource"') do set "NXAPI_SERVICE_STATUS=%%H"
  if not "!NXAPI_SERVICE_STATUS!"=="200" goto :service_unavailable
  echo nxapi authentication service is reachable.
  echo.
)

node "%NXAPI_JS%" nso auth
if errorlevel 1 goto :failed

echo.
echo Verifying the saved Nintendo Switch Online login...
node "%NXAPI_JS%" nso user >nul
if errorlevel 1 goto :verify_failed

echo.
echo Login and verification succeeded.
echo Restart from 机器人管理.cmd, then use /switch status in the QQ group.
pause
endlocal
exit /b 0

:service_unavailable
echo.
echo nxapi authentication service is unavailable ^(HTTP %NXAPI_SERVICE_STATUS%^).
echo This is an external service problem. No Nintendo login link was created.
echo Check https://nxapi-status.fancy.org.uk/ and run this script again later.
pause
endlocal
exit /b 1

:coral_disabled
echo.
echo nxapi configuration does not allow this client's Nintendo Switch Online login.
echo Check for a compatible update first: npm install --global nxapi@next
echo Keep existing login data. If already updated, check upstream service status.
pause
endlocal
exit /b 1

:config_unavailable
echo.
echo Unable to read or validate the nxapi remote configuration.
echo No Nintendo login attempt was made. Check the network and nxapi status page, then retry later.
pause
endlocal
exit /b 1

:verify_failed
echo.
echo Login data was not verified. Do not start the bot yet.
echo The actual nxapi error is shown above the "Specify --help" line.
echo If it says "Invalid state", close all old Nintendo login tabs and run this
echo script once. Open only the NEW URL printed by that same terminal window,
echo then paste its callback directly here. Never reuse or refresh an old login tab.
echo If the message mentions remote configuration, run:
echo   npm install --global nxapi@next
echo If it mentions fetch, OAuth, timeout, or the log stops at znca-auth,
echo check https://nxapi-status.fancy.org.uk/ and retry after services recover.
echo Then choose Switch login in 机器人管理.cmd again with a NEW callback link.
pause
endlocal
exit /b 1

:failed
echo.
echo Login failed. No success message will be shown until nxapi verifies the account.
echo The actual nxapi error is shown above the "Specify --help" line.
echo If it says "Invalid state", close all old Nintendo login tabs and run this
echo script once. Open only the NEW URL printed by that same terminal window,
echo then paste its callback directly here. Never reuse or refresh an old login tab.
echo If the message mentions remote configuration, run:
echo   npm install --global nxapi@next
echo If it mentions fetch, OAuth, timeout, or the log stops at znca-auth,
echo check https://nxapi-status.fancy.org.uk/ and retry after services recover.
echo Then choose Switch login in 机器人管理.cmd again with a NEW callback link.
pause
endlocal
exit /b 1
