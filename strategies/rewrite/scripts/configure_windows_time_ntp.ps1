[CmdletBinding()]
param(
    [ValidateRange(64, 86400)]
    [int]$PollIntervalSeconds = 300,

    [string[]]$Peers = @(
        "ntp.ntsc.ac.cn,0x9",
        "ntp.aliyun.com,0x9",
        "time.windows.com,0x9"
    ),

    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$W32tmPath = Join-Path $env:SystemRoot "System32\w32tm.exe"

function Invoke-W32tm {
    param([string[]]$W32Arguments)

    & $W32tmPath @W32Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "w32tm failed with exit code $($LASTEXITCODE): $($W32Arguments -join ' ')"
    }
}

function Show-TimeStatus {
    Write-Host "`n--- W32Time configuration ---"
    try {
        Invoke-W32tm -W32Arguments @("/query", "/configuration")
    }
    catch {
        Write-Warning "Could not read full configuration. Run -CheckOnly from an elevated PowerShell window for this detail."
    }

    Write-Host "`n--- W32Time status ---"
    try {
        Invoke-W32tm -W32Arguments @("/query", "/status")
    }
    catch {
        Write-Warning "Could not read W32Time status: $($_.Exception.Message)"
    }

    $ProbeServer = ($Peers[0] -split ",")[0]
    Write-Host "`n--- NTP offset samples: $ProbeServer ---"
    try {
        Invoke-W32tm -W32Arguments @("/stripchart", "/computer:$ProbeServer", "/samples:5", "/dataonly")
    }
    catch {
        Write-Warning "NTP offset sampling failed: $($_.Exception.Message)"
    }
}

if (-not (Test-Path -LiteralPath $W32tmPath)) {
    throw "w32tm.exe was not found: $W32tmPath"
}

if ($CheckOnly) {
    Show-TimeStatus
    return
}

$Computer = Get-CimInstance -ClassName Win32_ComputerSystem
if ($Computer.PartOfDomain) {
    throw "This computer is domain-joined. Use its domain time policy instead of configuring public NTP peers."
}

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated PowerShell window (Run as administrator)."
}

if ($Peers.Count -lt 2) {
    Write-Warning "Only one NTP peer is configured. Multiple peers provide a fallback."
}

$PeerList = $Peers -join " "
$ClientKey = "HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient"
if (-not (Test-Path -LiteralPath $ClientKey)) {
    throw "Windows Time NTP client registry key was not found: $ClientKey"
}

Write-Host "This will configure Windows Time to poll these NTP peers every $PollIntervalSeconds seconds:"
$Peers | ForEach-Object { Write-Host "  $_" }
Write-Host "The Windows Time service will be restarted and the clock will be resynchronized."
$Confirmation = Read-Host 'Type APPLY to continue'
if ($Confirmation -cne "APPLY") {
    Write-Host "Cancelled. No settings were changed."
    return
}

Invoke-W32tm -W32Arguments @(
    "/config",
    "/manualpeerlist:$PeerList",
    "/syncfromflags:manual",
    "/update"
)

Set-ItemProperty -LiteralPath $ClientKey -Name SpecialPollInterval -Value $PollIntervalSeconds
Set-Service -Name W32Time -StartupType Automatic
Restart-Service -Name W32Time

$ResyncFailed = $false
try {
    Invoke-W32tm -W32Arguments @("/resync", "/rediscover")
}
catch {
    $ResyncFailed = $true
    Write-Warning "Initial resynchronization failed: $($_.Exception.Message)"
}

Show-TimeStatus

if ($ResyncFailed) {
    throw "Configuration was applied, but initial resynchronization failed. Check network access to UDP port 123 and rerun with -CheckOnly."
}

Write-Host "`nConfiguration complete. Confirm SpecialPollInterval is $PollIntervalSeconds and the reported offset is stable."
