$udd = Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data'
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$chromeArgs = @(
    '--remote-debugging-port=9222',
    '--remote-allow-origins=*',
    "--user-data-dir=$udd",
    '--profile-directory=Default',
    '--no-first-run',
    '--no-default-browser-check',
    '--start-maximized',
    'https://etpgaz.gazprombank.ru/#com/procedure/index'
)
Write-Output "Запускаю: $chrome"
Write-Output "UDD: $udd"
Start-Process -FilePath $chrome -ArgumentList $chromeArgs
Start-Sleep -Seconds 10
$count = (Get-Process chrome -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Output "Chrome процессов: $count"
try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 5 -UseBasicParsing
    Write-Output "DevTools OK (HTTP $($r.StatusCode))"
} catch {
    Write-Output "DevTools DOWN: $($_.Exception.Message)"
}
