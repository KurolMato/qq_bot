$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$toolDirectory = Join-Path $projectRoot ".tools\$env:COMPUTERNAME\playwright"
$npmCommand = (Get-Command npm.cmd -ErrorAction Stop).Source
$nodeCommand = (Get-Command node.exe -ErrorAction Stop).Source
$edgePaths = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
    (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
)
if (-not ($edgePaths | Where-Object { Test-Path -LiteralPath $_ })) {
    throw 'Microsoft Edge was not found. Install Edge before setting up browser control.'
}
& $nodeCommand --version
if ($LASTEXITCODE -ne 0) { throw 'Node.js is not available.' }
New-Item -ItemType Directory -Force -Path $toolDirectory | Out-Null
# Pin the plugin and use the installed Edge; no separate Chromium download.
& $npmCommand install --prefix $toolDirectory --save-exact --no-audit --no-fund '@playwright/mcp@0.0.83'
if ($LASTEXITCODE -ne 0) { throw 'Playwright MCP installation failed.' }
Write-Host 'Browser plugin installed. Restart QQ Bot, then check /dsh status in private chat.'
