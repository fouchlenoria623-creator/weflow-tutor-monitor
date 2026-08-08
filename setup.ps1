param([switch]$Force)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$config = Join-Path $root 'config.local.json'
$example = Join-Path $root 'config.example.json'
if ((Test-Path -LiteralPath $config) -and -not $Force) {
    Write-Host "配置已存在：$config"
} else {
    Copy-Item -LiteralPath $example -Destination $config -Force
    Write-Host "已创建本地配置：$config"
}
foreach ($directory in @('state', 'reports', 'logs')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $root $directory) | Out-Null
}
Write-Host '下一步：编辑 config.local.json 的出发地、画像和群筛选，然后运行 .\check-setup.ps1'
