# 모니터 1 = /ops (실시간 관제), 모니터 2 = /insight (통계·민원·시스템). 모니터가 1대면 두 창을 같은 화면에 띄운다.
Add-Type -AssemblyName System.Windows.Forms
$screens = [System.Windows.Forms.Screen]::AllScreens | Sort-Object { $_.Bounds.X }
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
$chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
$browser = if ($chrome) { $chrome } else { $edge }
$pages = @("http://127.0.0.1:8000/ops", "http://127.0.0.1:8000/insight")
for ($i = 0; $i -lt 2; $i++) {
    $s = $screens[[Math]::Min($i, $screens.Count - 1)].Bounds
    $profile = Join-Path $env:TEMP "odortwin-screen$i"        # 창마다 별도 프로필 → 각 모니터에 독립 전체화면
    Start-Process $browser -ArgumentList "--user-data-dir=$profile", "--no-first-run", "--force-device-scale-factor=1", "--autoplay-policy=no-user-gesture-required", "--app=$($pages[$i])", "--window-position=$($s.X),$($s.Y)", "--window-size=$($s.Width),$($s.Height)", "--start-fullscreen"
    Start-Sleep 2
}
"screens: $($screens.Count), browser: $browser"
