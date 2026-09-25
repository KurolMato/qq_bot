@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\tools\select_python.cmd"

if not exist "%BOT_PYTHON%" (
  echo 找不到项目虚拟环境，请先在机器人管理器中完成依赖安装。
  pause
  exit /b 1
)

"%BOT_PYTHON%" -m qq_bot.nickname_batch import
echo.
echo 机器人无需重启，下一次列表和游玩通知会使用新昵称。
pause
