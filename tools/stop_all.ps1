$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Stop-MatchingProcess {
    param([string[]]$Patterns)
    try {
        $processes = Get-CimInstance Win32_Process | Where-Object {
            $commandLine = [string]$_.CommandLine
            foreach ($pattern in $Patterns) {
                if ($commandLine -like $pattern) { return $true }
            }
            return $false
        }
    } catch {
        Write-Warning "Unable to inspect running processes: $($_.Exception.Message)"
        return
    }
    foreach ($process in $processes) {
        if ($process.ProcessId -eq $PID) { continue }
        try {
            Stop-Process -Id $process.ProcessId -Force
            Write-Host "Stopped $($process.Name) PID $($process.ProcessId)"
        } catch {
            Write-Warning "Unable to stop $($process.Name) PID $($process.ProcessId): $($_.Exception.Message)"
        }
    }
}

# Stop the external watchdog first so it cannot relaunch the bot while shutting down.
Stop-MatchingProcess @('*qq_bot.napcat_watchdog*')
Stop-MatchingProcess @('*-m qq_bot.run*', '*uvicorn app.main:app*')

$napcatListeners = @(Get-NetTCPConnection -LocalPort 6099 -State Listen -ErrorAction SilentlyContinue)
$stoppedNapcat = @{}
foreach ($listener in $napcatListeners) {
    try {
        $process = Get-Process -Id $listener.OwningProcess
        if ($process.ProcessName -eq 'NapCatWinBootMain') {
            Stop-Process -Id $process.Id -Force
            $stoppedNapcat[$process.Id] = $true
            Write-Host "Stopped NapCat PID $($process.Id)"
        }
    } catch {
        Write-Warning "Unable to stop NapCat listener PID $($listener.OwningProcess): $($_.Exception.Message)"
    }
}

# The WebUI can be disabled or moved away from port 6099. Fall back to the
# same executable name used by start-all.cmd so stop-all still works.
foreach ($process in @(Get-Process -Name 'NapCatWinBootMain' -ErrorAction SilentlyContinue)) {
    if (-not $stoppedNapcat.ContainsKey($process.Id)) {
        try {
            Stop-Process -Id $process.Id -Force
            Write-Host "Stopped NapCat PID $($process.Id)"
        } catch {
            Write-Warning "Unable to stop NapCat PID $($process.Id): $($_.Exception.Message)"
        }
    }
}

Write-Host 'All project services are stopped.'
