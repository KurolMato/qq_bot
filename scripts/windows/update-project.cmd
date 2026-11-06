@echo off
setlocal EnableExtensions DisableDelayedExpansion
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
chcp 65001 >NUL

call "%PROJECT_ROOT%\tools\select_python.cmd"
if errorlevel 1 (
  echo ERROR: 找不到本机 Python 环境，请先在管理器中选择“首次配置 / 更新 Python 依赖”。
  exit /b 1
)

echo 请先在管理器中选择“完全关闭机器人”，再更新程序。
echo 将强制覆盖本机程序和默认资源；旧代码自动备份，配置、密钥、人设和运行数据保留。
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem Read the whole block before the updater can replace this batch file.
setlocal EnableDelayedExpansion
(
  "!BOT_PYTHON!" "!PROJECT_ROOT!\tools\update_project.py" %*
  exit /b !ERRORLEVEL!
)
