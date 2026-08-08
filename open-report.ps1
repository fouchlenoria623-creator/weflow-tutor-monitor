param(
    [string]$Report,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $root 'chrome-helper.ps1')

if (-not $Report) { $Report = Join-Path $root 'reports\latest.html' }
$chrome = Get-GoogleChromePath
$target = ConvertTo-ChromeTarget -Target $Report
if ($CheckOnly) {
    [pscustomobject]@{ Chrome = $chrome; Target = $target }
    return
}
Start-Process -FilePath $chrome -ArgumentList @('--new-tab', $target)
