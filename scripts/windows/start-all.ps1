$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '..\..')).Path
$computerVenv = Join-Path $projectRoot ".venvs\$env:COMPUTERNAME"
$python = Join-Path $computerVenv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
}

if (-not (Test-Path -LiteralPath $python)) {
    throw '找不到本机 Python 环境，请先通过机器人管理.cmd安装依赖。'
}

$api = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $projectRoot -PassThru -WindowStyle Hidden
$bot = Start-Process -FilePath $python -ArgumentList @('-m', 'qq_bot.run') -WorkingDirectory $projectRoot -PassThru -WindowStyle Hidden

Write-Host "网页/API 已启动，PID: $($api.Id)，地址: http://127.0.0.1:8000"
Write-Host "QQ Bot 已启动，PID: $($bot.Id)，反向 WebSocket: ws://127.0.0.1:8081/onebot/v11/ws"
Write-Host '关闭当前窗口不会终止后台进程；可在任务管理器中结束对应 python.exe。'
