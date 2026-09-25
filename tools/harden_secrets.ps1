param(
    [string]$SecretsPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'secrets')
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Host 'Skipping Windows ACL hardening on a non-Windows system.'
    exit 0
}

$fullPath = [IO.Path]::GetFullPath($SecretsPath)
if (-not (Test-Path -LiteralPath $fullPath)) {
    New-Item -ItemType Directory -Path $fullPath | Out-Null
}

$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$directRules = @(
    "*${userSid}:F",
    '*S-1-5-18:F',
    '*S-1-5-32-544:F'
)
$inheritRules = @(
    "*${userSid}:(OI)(CI)F",
    '*S-1-5-18:(OI)(CI)F',
    '*S-1-5-32-544:(OI)(CI)F'
)

& icacls.exe $fullPath /inheritance:r /T /C /Q | Out-Host
& icacls.exe $fullPath /grant:r $directRules[0] $directRules[1] $directRules[2] /T /C /Q | Out-Host
& icacls.exe $fullPath /grant $inheritRules[0] $inheritRules[1] $inheritRules[2] /Q | Out-Host
$allowedSids = @($userSid, 'S-1-5-18', 'S-1-5-32-544')
$items = @((Get-Item -LiteralPath $fullPath)) + @(Get-ChildItem -LiteralPath $fullPath -Force -Recurse)
foreach ($item in $items) {
    $acl = Get-Acl -LiteralPath $item.FullName
    if (-not $acl.AreAccessRulesProtected) {
        throw "Permissions are still inherited: $($item.FullName)"
    }
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $allowedSids) {
            throw "Unexpected account still has access to secrets: $sid"
        }
    }
}

Write-Host "Secrets permissions restricted to the current user, SYSTEM and Administrators: $fullPath"
