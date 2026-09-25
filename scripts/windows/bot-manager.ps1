$ErrorActionPreference = 'Continue'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$scriptDirectory = Join-Path $projectRoot 'scripts\windows'

function Invoke-ProjectCommand([string]$name) {
    $path = Join-Path $scriptDirectory $name
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Host "缺少脚本：$path" -ForegroundColor Red
        return
    }
    & $path
}

function Wait-ForUser {
    Write-Host
    [void](Read-Host '按回车返回主菜单')
}

while ($true) {
    Clear-Host
    Write-Host '============================================================'
    Write-Host '                 Video Analysis Bot 管理器'
    Write-Host '============================================================'
    Write-Host '  1. 启动整套机器人'
    Write-Host '  2. 完全关闭机器人'
    Write-Host '  3. 打开管理后台'
    Write-Host '  4. 只重启 QQ Bot'
    Write-Host '  5. 首次配置 / 更新 Python 依赖'
    Write-Host '  6. 安装 Windows 基础软件'
    Write-Host '  7. 登录 Switch 观察账号'
    Write-Host '  8. 登录 PSN 观察账号'
    Write-Host '  9. 配置 Steam API Key'
    Write-Host ' 10. 配置 SteamGridDB Key'
    Write-Host ' 11. 登录 Xbox 观察账号'
    Write-Host ' 12. 导出昵称表'
    Write-Host ' 13. 导入昵称表'
    Write-Host ' 14. 从 GitHub 更新项目'
    Write-Host ' 15. 打开日志目录'
    Write-Host '  0. 退出'
    Write-Host '============================================================'

    $choice = (Read-Host '请输入序号').Trim()
    switch ($choice) {
        '1' { Invoke-ProjectCommand 'start-all.cmd'; Wait-ForUser }
        '2' { Invoke-ProjectCommand 'stop-all.cmd' }
        '3' { Invoke-ProjectCommand 'open-admin.cmd'; Wait-ForUser }
        '4' { Invoke-ProjectCommand 'restart-qq-bot.cmd'; Wait-ForUser }
        '5' { Invoke-ProjectCommand 'setup-laptop.cmd'; Wait-ForUser }
        '6' { Invoke-ProjectCommand 'install-laptop-prerequisites.cmd'; Wait-ForUser }
        '7' { Invoke-ProjectCommand 'switch-login.cmd' }
        '8' { Invoke-ProjectCommand 'ps5-login.cmd' }
        '9' { Invoke-ProjectCommand 'steam-login.cmd' }
        '10' { Invoke-ProjectCommand 'steamgriddb-login.cmd' }
        '11' { Invoke-ProjectCommand 'xbox-login.cmd' }
        '12' { Invoke-ProjectCommand 'nickname-export.cmd' }
        '13' { Invoke-ProjectCommand 'nickname-import.cmd' }
        '14' { Invoke-ProjectCommand 'update-project.cmd'; Wait-ForUser }
        '15' {
            $dataDirectory = Join-Path $projectRoot 'data'
            New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
            Start-Process explorer.exe -ArgumentList $dataDirectory
            Wait-ForUser
        }
        '0' { return }
        default {
            Write-Host '无效序号，请重新输入。' -ForegroundColor Yellow
            Start-Sleep -Seconds 1
        }
    }
}
