# OdorTwin 전체 정지. 다시 켜면(.\scripts\start_all.ps1) 재생이 2024-11-10 부터 다시 시작되며 시연 데이터가 초기화된다.
# (2023-02~2025-04 이력, 레이크하우스 이력 테이블, 센서 메타는 유지)
$demo = Split-Path -Parent $PSScriptRoot
Set-Location $demo
& "$PSScriptRoot\services.ps1" stop
docker compose stop
