param(
    [string]$TaskName = "Cytrade MainSealFollow Monitor",
    [string]$StartTime = "08:50",
    [string]$RepoRoot = "",
    [string]$BatchPath = ""
)

$ErrorActionPreference = "Stop"

function Resolve-HhMm {
    param([string]$Value)
    if ($Value -notmatch '^\d{2}:\d{2}$') {
        throw "Invalid StartTime format: $Value. Expected HH:MM."
    }
    return $Value
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptDir ".."))
}
if ([string]::IsNullOrWhiteSpace($BatchPath)) {
    $BatchPath = Join-Path $scriptDir "start_main_seal_follow_monitor.bat"
}
if (-not (Test-Path -LiteralPath $BatchPath)) {
    throw "Batch launcher not found: $BatchPath"
}

$StartTime = Resolve-HhMm -Value $StartTime
$triggerTime = [datetime]::ParseExact($StartTime, "HH:mm", $null)
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatchPath`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $triggerTime
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Start cytrade MainSealFollow dry-run monitoring session on trading mornings." `
    -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Start time: $StartTime"
Write-Host "Batch path: $BatchPath"
