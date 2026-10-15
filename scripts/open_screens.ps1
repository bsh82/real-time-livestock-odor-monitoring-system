# 모니터 1 = /ops (실시간 관제), 모니터 2 = /insight (통계·민원·시스템). 모니터가 1대면 두 창을 같은 화면에 띄운다.
# 화면 배율: 모니터마다 실제 해상도 가로 / 1920 → 어느 모니터·Windows 배율에서도 화면이 1920 기준 배치 그대로 꽉 차게
#   (예: 1920×1080 → 1, 4K → 2). 창 위치·크기도 그 배율 기준(물리 ÷ 배율)으로 준다. ODORTWIN_SCREEN_DEBUG=1 이면 창마다 원격 디버깅 포트(9381, 9382)를 연다 (점검용).
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public static class OdorDisplay {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  public struct DEVMODE {
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
    public short dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra; public int dmFields;
    public int dmPositionX, dmPositionY, dmDisplayOrientation, dmDisplayFixedOutput;
    public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
    public short dmLogPixels; public int dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency;
    public int dmICMMethod, dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2, dmPanningWidth, dmPanningHeight;
  }
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern bool EnumDisplaySettings(string dev, int mode, ref DEVMODE dm);
  public static int[] Physical(string dev) {                                 // 물리 픽셀 [x, y, 가로, 세로]
    var d = new DEVMODE(); d.dmSize = (short)Marshal.SizeOf(typeof(DEVMODE));
    return EnumDisplaySettings(dev, -1, ref d) ? new[] { d.dmPositionX, d.dmPositionY, d.dmPelsWidth, d.dmPelsHeight } : null;   // -1 = ENUM_CURRENT_SETTINGS
  }
}
"@
$screens = [System.Windows.Forms.Screen]::AllScreens | Sort-Object { $_.Bounds.X }
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
$chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
$browser = if ($chrome) { $chrome } else { $edge }
$pages = @("http://127.0.0.1:8000/ops", "http://127.0.0.1:8000/insight")
# 이전에 띄운 관제 창이 남아 있으면 닫는다 (같은 프로필이면 새 창이 옛 프로세스·옛 배율로 열리므로)
Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" | Where-Object { $_.CommandLine -like '*odortwin-screen*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 1
$info = @()
for ($i = 0; $i -lt 2; $i++) {
    $scr = $screens[[Math]::Min($i, $screens.Count - 1)]; $s = $scr.Bounds
    $ph = [OdorDisplay]::Physical($scr.DeviceName); if (-not $ph) { $ph = @($s.X, $s.Y, $s.Width, $s.Height) }
    $k = [Math]::Max(0.5, [Math]::Round($ph[2] / 1920.0, 3)); $scale = $k.ToString([Globalization.CultureInfo]::InvariantCulture)
    $pos = "$([int][Math]::Ceiling($ph[0] / $k)),$([int][Math]::Ceiling($ph[1] / $k))"; $size = "$([int]($ph[2] / $k)),$([int]($ph[3] / $k))"
    $profile = Join-Path $env:TEMP "odortwin-screen$i"        # 창마다 별도 프로필 → 각 모니터에 독립 전체화면
    $args = @("--user-data-dir=$profile", "--no-first-run", "--force-device-scale-factor=$scale", "--autoplay-policy=no-user-gesture-required", "--app=$($pages[$i])",
              "--window-position=$pos", "--window-size=$size", "--start-fullscreen")
    if ($env:ODORTWIN_SCREEN_DEBUG) { $args += "--remote-debugging-port=$(9381 + $i)" }
    Start-Process $browser -ArgumentList $args
    $info += "$($pages[$i] -replace '.*/', '/') -> $($scr.DeviceName) $($ph[2])x$($ph[3]) at $($ph[0]),$($ph[1]) x$scale (window $pos $size)"
    Start-Sleep 2
}
"screens: $($screens.Count), browser: $browser"
$info
