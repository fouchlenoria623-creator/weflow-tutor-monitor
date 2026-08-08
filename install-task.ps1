param(
    [string]$TaskName = 'WeFlow Tutor Monitor',
    [string]$Config = $env:TUTOR_MONITOR_CONFIG
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Config) { $Config = Join-Path $root 'config.local.json' }
if (-not (Test-Path -LiteralPath $Config)) {
    throw "未找到配置 $Config。请先运行 .\setup.ps1"
}

$configData = Get-Content -Raw -Encoding UTF8 -LiteralPath $Config | ConvertFrom-Json
$startHour = if ($null -ne $configData.active_hours.start) { [int]$configData.active_hours.start } else { 10 }
$endHour = if ($null -ne $configData.active_hours.end) { [int]$configData.active_hours.end } else { 21 }
$interval = if ($null -ne $configData.scan_interval_minutes) { [int]$configData.scan_interval_minutes } else { 60 }
if ($startHour -lt 0 -or $startHour -gt 23 -or $endHour -lt $startHour -or $endHour -gt 23) {
    throw 'active_hours 必须满足 0 <= start <= end <= 23。'
}
if ($interval -lt 15 -or $interval -gt 1440) { throw '计划任务间隔必须在 15 到 1440 分钟之间。' }

$runner = Join-Path $root 'run-monitor.ps1'
$actionArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -Config `"$Config`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $actionArgs
$cursor = [datetime]::Today.AddHours($startHour)
$last = [datetime]::Today.AddHours($endHour).AddMinutes(59)
$triggers = @()
while ($cursor -le $last) {
    $triggers += New-ScheduledTaskTrigger -Daily -At $cursor
    $cursor = $cursor.AddMinutes($interval)
}
if ($triggers.Count -gt 48) { throw '触发器超过 48 个，请缩短运行时段或增大扫描间隔。' }

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$description = "Read authorized tutoring-group messages through the local WeFlow API every $interval minutes from ${startHour}:00 through ${endHour}:59."
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal -Description $description -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
