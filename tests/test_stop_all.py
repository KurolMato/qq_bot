"""Exercise the Windows shutdown script with process commands fully mocked."""
import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest


POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell required")


HARNESS = r"""
$global:stopAllTestScenario = $env:STOP_ALL_TEST_SCENARIO
$global:stopAllTestCalls = [System.Collections.Generic.List[int]]::new()

function Get-CimInstance {
    param($ClassName)
    @(
        [pscustomobject]@{ ProcessId=100; Name='pythonw.exe'; CommandLine='pythonw -m qq_bot.napcat_watchdog' }
        [pscustomobject]@{ ProcessId=200; Name='python.exe'; CommandLine='python -m qq_bot.run' }
        [pscustomobject]@{ ProcessId=201; Name='python.exe'; CommandLine='python -m qq_bot.run' }
        [pscustomobject]@{ ProcessId=999; Name='python.exe'; CommandLine='python unrelated.py' }
    )
}

function Get-NetTCPConnection {
    param($LocalPort, $State, $ErrorAction)
    [pscustomobject]@{ OwningProcess=300 }
    [pscustomobject]@{ OwningProcess=900 }
}

function Get-Process {
    [CmdletBinding()]
    param($Id, $Name)
    if ($Id -eq 300 -and $global:stopAllTestScenario -eq 'listener-gone') {
        $errorRecord = [System.Management.Automation.ErrorRecord]::new(
            [System.ArgumentException]::new('Process already exited'),
            'NoProcessFoundForGivenId',
            [System.Management.Automation.ErrorCategory]::ObjectNotFound, $Id
        )
        $PSCmdlet.ThrowTerminatingError($errorRecord)
    }
    if ($Id) {
        $processName = if ($Id -eq 900) { 'OtherApp' } else { 'NapCatWinBootMain' }
        return [pscustomobject]@{ Id=$Id; ProcessName=$processName }
    }
    @(
        if ($global:stopAllTestScenario -notin @('listener-gone', 'listener-stop-gone')) {
            [pscustomobject]@{ Id=300; ProcessName='NapCatWinBootMain' }
        }
        [pscustomobject]@{ Id=301; ProcessName='NapCatWinBootMain' }
    )
}

function Stop-Process {
    [CmdletBinding()]
    param($Id, [switch]$Force)
    $global:stopAllTestCalls.Add([int]$Id)
    $missing = ($Id -eq 201 -and $global:stopAllTestScenario -eq 'python-child-gone') -or
               ($Id -eq 300 -and $global:stopAllTestScenario -eq 'listener-stop-gone') -or
               ($Id -eq 301 -and $global:stopAllTestScenario -eq 'napcat-fallback-gone')
    if ($missing) {
        $errorRecord = [System.Management.Automation.ErrorRecord]::new(
            [System.ArgumentException]::new('Process already exited'),
            'NoProcessFoundForGivenId',
            [System.Management.Automation.ErrorCategory]::ObjectNotFound, $Id
        )
        $PSCmdlet.ThrowTerminatingError($errorRecord)
    }
    if ($Id -eq 200 -and $global:stopAllTestScenario -eq 'access-denied') {
        $errorRecord = [System.Management.Automation.ErrorRecord]::new(
            [System.UnauthorizedAccessException]::new('Access denied'),
            'CouldNotStopProcess',
            [System.Management.Automation.ErrorCategory]::PermissionDenied, $Id
        )
        $PSCmdlet.ThrowTerminatingError($errorRecord)
    }
}

& $env:STOP_ALL_TEST_SCRIPT
Write-Output ('STOP_CALLS=' + ($global:stopAllTestCalls -join ','))
"""


@pytest.mark.parametrize("scenario", [
    "normal", "python-child-gone", "listener-gone",
    "listener-stop-gone", "napcat-fallback-gone", "access-denied",
])
def test_shutdown_handles_exit_races_and_keeps_real_failures(scenario: str) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "stop_all.ps1"
    encoded = base64.b64encode(HARNESS.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [
            POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-OutputFormat", "Text", "-EncodedCommand", encoded,
        ],
        env={
            **os.environ,
            "STOP_ALL_TEST_SCRIPT": str(script),
            "STOP_ALL_TEST_SCENARIO": scenario,
        },
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    if scenario == "access-denied":
        assert "Unable to stop python.exe PID 200" in output
        assert "Access denied" in output
    else:
        assert "WARNING" not in output
        assert "Unable to stop" not in output
    assert "All project services are stopped." in output
    calls = next(line for line in result.stdout.splitlines() if line.startswith("STOP_CALLS="))
    process_ids = [int(value) for value in calls.split("=", 1)[1].split(",")]
    # The watchdog must stop before the bot, and unrelated processes stay alone.
    assert process_ids[:3] == [100, 200, 201]
    assert 900 not in process_ids
    assert 999 not in process_ids
    assert process_ids.count(300) == (0 if scenario == "listener-gone" else 1)
    assert process_ids.count(301) == 1
