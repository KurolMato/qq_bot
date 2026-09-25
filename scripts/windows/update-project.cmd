@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
chcp 65001 >NUL

where git.exe >NUL 2>NUL
if errorlevel 1 (
  echo ERROR: 找不到 Git，请先在管理器中选择“安装 Windows 基础软件”。
  exit /b 1
)

if not exist ".git" (
  echo ERROR: 当前项目不是通过 Git 克隆的，无法直接更新。
  echo 请阅读 README.md 的“从 GitHub 更新”章节。
  exit /b 1
)

for /f "delims=" %%S in ('git status --porcelain --untracked-files=no') do (
  echo ERROR: 当前代码有尚未提交的修改，为避免覆盖，已取消更新。
  echo 请先运行 git status 检查修改。
  exit /b 1
)

echo 正在从 GitHub 获取更新...
git pull --ff-only
if errorlevel 1 (
  echo ERROR: 更新失败，请检查网络、GitHub 登录状态和上方错误。
  exit /b 1
)

echo.
echo 更新完成。如依赖清单有变化，请在管理器中选择“首次配置 / 更新 Python 依赖”。
exit /b 0
