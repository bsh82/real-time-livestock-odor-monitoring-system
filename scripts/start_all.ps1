# OdorTwin 원클릭 기동
#   .\scripts\start_all.ps1            # 전체 기동 (이미 켜진 것은 그대로). 시뮬레이터가 꺼져 있었으면 재생이 처음부터 다시
#                                      # 시작되므로 데이터 초기화(-Reset)를 자동으로 함께 한다.
#   .\scripts\start_all.ps1 -Reset     # 시연 데이터 초기화 후 기동 (재생을 2024-11-10 00:00 부터 다시)
#   .\scripts\start_all.ps1 -Screens   # 기동 후 모니터 1 = /ops, 모니터 2 = /insight 전체화면
param([switch]$Reset, [switch]$Screens)
$ErrorActionPreference = "Continue"
$demo = Split-Path -Parent $PSScriptRoot
Set-Location $demo
function Step($m) { Write-Host ("[{0}] {1}" -f (Get-Date -Format HH:mm:ss), $m) -ForegroundColor Cyan }
function WaitFor($what, $sec, [scriptblock]$ok) {
    for ($i = 0; $i -lt $sec; $i += 3) { if (& $ok) { Write-Host "  $what ready"; return $true }; Start-Sleep 3 }
    Write-Host "  $what NOT ready after ${sec}s" -ForegroundColor Red; return $false
}

# 1. Docker Desktop
Step "Docker"
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    # PC 재부팅 등 비정상 종료 뒤 남은 유닉스 소켓 파일 때문에 Docker Desktop 백엔드가 멈추는 문제 (2026-10-14 겪음).
    # Windows 에서는 지울 수 없어 WSL 로 지운다. Docker 가 꺼져 있을 때만 한다.
    Get-Process | Where-Object { $_.ProcessName -in @("Docker Desktop", "com.docker.backend", "docker-ai") } | Stop-Process -Force -ErrorAction SilentlyContinue
    foreach ($r in "$env:LOCALAPPDATA\Docker\run", "$env:LOCALAPPDATA\docker-secrets-engine") {
        Get-ChildItem -LiteralPath $r -Force -ErrorAction SilentlyContinue |
            Where-Object { -not $_.PSIsContainer -and ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -and $_.Length -eq 0 } |
            ForEach-Object { $lp = ($_.FullName -replace '^C:', '/mnt/c') -replace '\\', '/'; wsl -d Ubuntu -- rm -f "$lp" 2>$null; Write-Host "  removed stale socket $($_.Name)" }
    }
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (-not (WaitFor "Docker daemon" 240 { docker info *> $null; $LASTEXITCODE -eq 0 })) {
        Write-Host "Docker Desktop 이 켜지지 않았습니다. Docker Desktop 창의 오류를 확인하세요 (Reset to factory defaults 는 누르지 말 것: DB 볼륨이 지워짐)." -ForegroundColor Red
        exit 1
    }
}

# 2. 컨테이너
Step "docker compose up"
docker compose up -d 2>&1 | Out-Null
$null = WaitFor "TimescaleDB" 90 { docker compose exec -T timescaledb pg_isready -U odor *> $null; $LASTEXITCODE -eq 0 }
$null = WaitFor "Kafka" 120 { docker compose exec -T kafka kafka-topics --bootstrap-server kafka:29092 --list *> $null; $LASTEXITCODE -eq 0 }
$null = WaitFor "Trino" 120 { try { (Invoke-RestMethod http://127.0.0.1:8088/v1/info -TimeoutSec 3).starting -eq $false } catch { $false } }
$null = WaitFor "Flink" 90 { try { (Invoke-RestMethod http://127.0.0.1:8081/overview -TimeoutSec 3).taskmanagers -ge 1 } catch { $false } }
# Airflow 메타데이터 DB (예전 볼륨에는 없을 수 있음) → 만든 뒤 Airflow 를 다시 띄운다
$hasAf = docker compose exec -T timescaledb psql -U odor -d odor -tAc "SELECT 1 FROM pg_database WHERE datname='airflow'"
if (-not "$hasAf".Trim()) { docker compose exec -T timescaledb psql -U odor -d odor -c "CREATE DATABASE airflow" | Out-Null; docker compose restart airflow *> $null; Write-Host "  airflow DB created" }
$null = WaitFor "Airflow" 180 { try { (Invoke-RestMethod http://127.0.0.1:8090/api/v2/monitor/health -TimeoutSec 3).scheduler.status -eq "healthy" } catch { $false } }

# 3. 토픽·버킷 (있으면 그대로)
Step "Kafka topics / S3 bucket"
foreach ($t in "odor.raw","odor.clean","odor.agg10m","odor.emission","odor.alert","device.status","cmd.down","cmd.ack","sim.clock","control.sim","weather.wind","odor.field","odor.dlq","odor.complaint","ops.metrics") {
    docker compose exec -T kafka kafka-topics --bootstrap-server kafka:29092 --create --if-not-exists --topic $t --partitions 3 --replication-factor 1 *> $null
}
docker compose exec -T seaweedfs sh -c "echo 's3.bucket.create -name lake' | weed shell -master=localhost:9333" *> $null

# 4. 초기화: 시뮬레이터는 늘 2024-11-10 00:00 부터 재생하므로, 꺼져 있었다면 이전 데이터와 겹치지 않게 반드시 초기화한다
$simRunning = [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*services.simulator.main*' })
if (-not $simRunning -and -not $Reset) { Write-Host '  simulator was not running -> replay restarts from 2024-11-10, so data reset is applied' -ForegroundColor Yellow; $Reset = $true }
if ($Reset) {
    Step "Reset demo data"
    & "$PSScriptRoot\services.ps1" stop | Out-Null
    docker compose exec -T timescaledb psql -U odor -d odor -c "TRUNCATE sensor_raw, emission, sensor_agg10m, alert_events, alarms, alarm_log, dispersion, complaints, weather" | Out-Null
    docker compose exec -T trino trino --execute "DELETE FROM lake.bronze.sensor_stream" *> $null
    Get-ChildItem "$demo\runtime\fields" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
}

# 5. Flink 작업 (돌고 있지 않으면 제출)
Step "Flink job"
$live = { @(try { (Invoke-RestMethod http://127.0.0.1:8081/jobs/overview -TimeoutSec 5).jobs } catch { @() }) |
          Where-Object { $_.name -eq "odortwin-stream" -and $_.state -notin @("FINISHED", "CANCELED", "FAILED") } }
$running = & $live
if ($Reset -and $running) {
    foreach ($j in $running) { Invoke-RestMethod -Method Patch "http://127.0.0.1:8081/jobs/$($j.jid)?mode=cancel" | Out-Null }
    $null = WaitFor "Flink job cancel" 60 { -not (& $live) }
    $running = $null
}
if (-not $running) {
    $env:MSYS_NO_PATHCONV = "1"
    docker compose exec -T flink-jobmanager ./bin/sql-client.sh -f /opt/flink/jobs/odortwin_stream.sql 2>&1 | Select-String "Job ID" | ForEach-Object { "  $_" }
} else { Write-Host "  already running" }

# 6. 호스트 서비스
Step "Host services"
& "$PSScriptRoot\services.ps1" start
$null = WaitFor "API" 90 { try { (Invoke-WebRequest http://127.0.0.1:8000/api/meta -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 } catch { $false } }
& "$PSScriptRoot\services.ps1" status

# 7. 화면 (선택): 모니터 1 = 실시간 관제, 모니터 2 = 통계·민원
if ($Screens) {
    Step "Screens"
    & "$PSScriptRoot\open_screens.ps1"
}
Step "done — http://localhost:8000/ops , http://localhost:8000/insight · Airflow http://127.0.0.1:8090"
