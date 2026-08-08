param(
    [string]$Title,
    [string]$Body,
    [string]$Report
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $root 'chrome-helper.ps1')
$chrome = Get-GoogleChromePath
$reportTarget = ConvertTo-ChromeTarget -Target $Report
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Information
$icon.Visible = $true
$icon.BalloonTipTitle = $Title
$icon.BalloonTipText = $Body
$icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
$icon.add_BalloonTipClicked({
    Start-Process -FilePath $chrome -ArgumentList @('--new-tab', $reportTarget)
}.GetNewClosure())
$icon.ShowBalloonTip(10000)
Start-Sleep -Seconds 12
$icon.Dispose()
