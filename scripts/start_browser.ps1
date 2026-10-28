param(
    [int]$Port = 9222,
    [ValidateSet('Edge', 'Chrome')][string]$Browser = 'Edge',
    [switch]$OpenJobs
)

$profileName = $Browser.ToLowerInvariant()
$profileDir = Join-Path $env:LOCALAPPDATA ("bossgreet\$profileName-profile")
New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
$browserExe = if ($Browser -eq 'Edge') { 'msedge.exe' } else { 'chrome.exe' }
$url = if ($OpenJobs) {
    'https://www.zhipin.com/web/geek/jobs'
} else {
    # 首次启动只到登录页；账号、密码、扫码和安全校验只能由使用者完成。
    'https://www.zhipin.com/web/user/?ka=header-login'
}
$arguments = @(
    "--remote-debugging-port=$Port"
    "--user-data-dir=`"$profileDir`""
    $url
)
Start-Process -FilePath $browserExe -ArgumentList $arguments
if ($OpenJobs) {
    Write-Host "浏览器已在同一专用配置中打开职位页。"
} else {
    Write-Host "浏览器已打开 BOSS 登录页。请完成登录后，在本地工作目录重新运行 bossgreet preview。"
}
