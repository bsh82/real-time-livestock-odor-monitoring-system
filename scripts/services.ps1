# OdorTwin 호스트 서비스 관리 (파이썬 서비스 5개 + 화면). 레이크하우스 배치는 Airflow 컨테이너가 돌린다.
#   .\scripts\services.ps1 start [이름...]   # 기본: bridge simulator weather engine api
#   .\scripts\services.ps1 start batch       # Airflow 를 쓸 수 없을 때만: 예비 배치 실행기
#   .\scripts\services.ps1 stop  [이름...]
#   .\scripts\services.ps1 status
param([string]$cmd = "status", [Parameter(ValueFromRemainingArguments = $true)][string[]]$names = @())
$demo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $demo ".conda\python.exe"
$all = @("bridge", "simulator", "weather", "engine", "api")
$extra = @("batch")                                   # 예비 (Airflow 대체)
if ($names.Count -eq 0) { $names = $all }

function Find-Svc($n) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*services.$n.main*" }
}
switch ($cmd) {
    "start" {
        New-Item -ItemType Directory -Force (Join-Path $demo "logs") | Out-Null
        foreach ($n in $names) {
            if (Find-Svc $n) { "$n already running" }
            else {
                $log = Join-Path $demo "logs\$n.log"
                try {
                    Start-Process -FilePath $py -ArgumentList "-m", "services.$n.main" -WorkingDirectory $demo -WindowStyle Hidden `
                        -RedirectStandardOutput $log -RedirectStandardError "$log.err" -ErrorAction Stop
                    "started $n"
                } catch { "failed $n : $($_.Exception.Message)" }
                Start-Sleep -Milliseconds 800
            }
        }
    }
    "stop" {
        foreach ($n in $names) { Find-Svc $n | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "stopped $n ($($_.ProcessId))" } }
    }
    "status" {
        foreach ($n in $all + $extra) { $p = Find-Svc $n; if ($p) { "{0,-10} running  pid {1}" -f $n, ($p.ProcessId -join ',') } else { "{0,-10} stopped" -f $n } }
    }
}
