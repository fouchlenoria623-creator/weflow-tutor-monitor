param(
    [string]$Config = $env:TUTOR_MONITOR_CONFIG,
    [switch]$ListGroups
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Config) { $Config = Join-Path $root 'config.local.json' }
$arguments = @((Join-Path $root 'monitor.py'), '--config', $Config)
if ($ListGroups) { $arguments += '--list-groups' } else { $arguments += '--check' }

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    & $venvPython @arguments
} elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
    & py.exe -3 @arguments
} elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
    & python.exe @arguments
} else {
    throw '未找到 Python 3。'
}
exit $LASTEXITCODE
