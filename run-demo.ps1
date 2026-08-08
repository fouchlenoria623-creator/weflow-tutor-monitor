$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$output = Join-Path $root 'demo-output'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$arguments = @((Join-Path $root 'demo.py'), '--out-dir', $output)
if (Test-Path -LiteralPath $venvPython) {
    & $venvPython @arguments
} elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
    & py.exe -3 @arguments
} elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
    & python.exe @arguments
} else {
    throw '未找到 Python 3。请安装 Python 3.10+，或在项目中创建 .venv。'
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$html = Join-Path $output 'latest.html'
& (Join-Path $root 'open-report.ps1') -Report $html
