param(
    [switch]$Force,
    [string]$Config = $env:TUTOR_MONITOR_CONFIG
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Config) { $Config = Join-Path $root 'config.local.json' }
if (-not (Test-Path -LiteralPath $Config)) {
    Write-Error "未找到配置 $Config。请先运行 .\setup.ps1"
    exit 2
}

$dataRoot = if ($env:TUTOR_MONITOR_DATA_DIR) { $env:TUTOR_MONITOR_DATA_DIR } else { $root }
$logDir = Join-Path $dataRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'monitor.log'
$settings = Get-Content -Raw -Encoding UTF8 -LiteralPath $Config | ConvertFrom-Json
$startHour = if ($null -ne $settings.active_hours.start) { [int]$settings.active_hours.start } else { 10 }
$endHour = if ($null -ne $settings.active_hours.end) { [int]$settings.active_hours.end } else { 21 }

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$pythonArgs = @()
if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
} elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
    $python = (Get-Command py.exe).Source
    $pythonArgs = @('-3')
} elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
    $python = (Get-Command python.exe).Source
} else {
    Write-Error '未找到 Python 3。请安装 Python 3.10+，或在项目中创建 .venv。'
    exit 2
}

$baiduMapAk = [Environment]::GetEnvironmentVariable('BAIDU_MAP_AK', 'User')
if ($baiduMapAk) { $env:BAIDU_MAP_AK = $baiduMapAk }
$hour = (Get-Date).Hour
if (-not $Force -and ($hour -lt $startHour -or $hour -gt $endHour)) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] skipped outside ${startHour}:00-${endHour}:59" |
        Out-File -LiteralPath $log -Append -Encoding utf8
    exit 0
}

$mutex = New-Object System.Threading.Mutex($false, 'Global\WeFlowTutorMonitor')
if (-not $mutex.WaitOne(0)) { exit 0 }
$exitCode = 1
try {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] start" | Out-File -LiteralPath $log -Append -Encoding utf8
    $output = & $python @pythonArgs (Join-Path $root 'monitor.py') --config $Config 2>&1
    $exitCode = $LASTEXITCODE
    if ($output) { $output | Out-File -LiteralPath $log -Append -Encoding utf8 }
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] done exit=$exitCode" |
        Out-File -LiteralPath $log -Append -Encoding utf8
} catch {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] error: $($_.Exception.Message)" |
        Out-File -LiteralPath $log -Append -Encoding utf8
    $exitCode = 1
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
exit $exitCode
