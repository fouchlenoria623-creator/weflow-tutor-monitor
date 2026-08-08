function Get-GoogleChromePath {
    $candidates = @(
        $env:CHROME_PATH,
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

    $chrome = $candidates | Select-Object -First 1
    if (-not $chrome) {
        $command = Get-Command chrome.exe -ErrorAction SilentlyContinue
        if ($command) { $chrome = $command.Source }
    }
    if (-not $chrome) {
        throw '未找到 Google Chrome。请安装 Chrome，或设置 CHROME_PATH。为避免意外打开 Edge，本项目不会回退到系统默认浏览器。'
    }
    return (Resolve-Path -LiteralPath $chrome).Path
}

function ConvertTo-ChromeTarget {
    param([Parameter(Mandatory = $true)][string]$Target)

    if (Test-Path -LiteralPath $Target) {
        return ([System.Uri](Resolve-Path -LiteralPath $Target).Path).AbsoluteUri
    }
    $uri = $null
    if ([System.Uri]::TryCreate($Target, [System.UriKind]::Absolute, [ref]$uri) -and
        $uri.Scheme -in @('file', 'http', 'https')) {
        return $uri.AbsoluteUri
    }
    throw "报告不存在或地址无效：$Target"
}

function Open-InGoogleChrome {
    param([Parameter(Mandatory = $true)][string]$Target)

    $chrome = Get-GoogleChromePath
    $browserTarget = ConvertTo-ChromeTarget -Target $Target
    Start-Process -FilePath $chrome -ArgumentList @('--new-tab', $browserTarget)
}
