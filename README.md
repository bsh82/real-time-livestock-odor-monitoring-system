# OdorTwin — 제주 축산악취 실시간 관제 시스템

축산 농가 악취센서(NH₃) 스트림을 실시간으로 처리하고, 지형·건물을 반영한 **신경 연산자(U-FNO / FNO) 확산 대리모델**로 악취가 어디까지 퍼지는지 매 순간 예측해 보여 주는 관제 시스템이다.
졸업작품 「기체확산 예측을 위한 가우시안 플룸 기반 PINN 모델」을 발전시킨 전시용 구현이며, CFD(OpenFOAM RANS) 로 수십 분 걸리는 계산을 GPU 에서 1~2초 안에 끝낸다.

> 이 저장소에는 **코드만** 있다. 센서 실측 자료, 농가 이름·위치, 입력 래스터, 학습된 모델, 시연 설정은 공개하지 않는다 (아래 [공개하지 않는 것](#공개하지-않는-것)).

## 화면

| 모니터 1 — 실시간 관제 (`/ops`) | 모니터 2 — 통계 · 민원 · 시스템 (`/insight`) |
|---|---|
| 권역(클러스터) 목록 · 확산 지도 · 센서 상태 · 경보 처리(발생 → 확인 → 저감 조치 → 해제) · 선택 농가 추이 · 농장별 3D 확산(지형·건물·바람 유선) | 시연 진행 · 민원 역추적(기여 농가 분해) · 가상 실험(GPU vs CFD) · 데이터 흐름 · 레이크하우스(bronze/silver/gold) |

## 구성

```
[센서 게이트웨이 시뮬레이터] --MQTT--> Mosquitto --> bridge --> Kafka
                                                     │
                     Flink SQL (정제 · 10분 집계 · 배출량 · MATCH_RECOGNIZE 경보) ──> TimescaleDB
                                                     │
 weather(JMA MSM 바람) ──> Kafka ──> engine (GPU: U-FNO+F 바람장 → FNO 농도장, 권역 20개)
                                                     │
                             api (FastAPI · WebSocket) ──> web (React · deck.gl · three.js)
 Airflow DAG ──> Trino ──> Iceberg (SeaweedFS S3)  bronze → silver → gold (5분 주기, 이력 통계·유지 관리는 매일)
```

| 폴더 | 내용 |
|---|---|
| `services/simulator` | 실측 이력을 ×60 으로 재생하는 MQTT 게이트웨이 107개 (LWT, 저감 명령 응답) |
| `services/bridge` | MQTT ↔ Kafka |
| `infra/flink/jobs` | Flink SQL 스트림 작업 (정제·집계·배출량·경보) |
| `services/engine` | 확산 대리모델 (`model.py`), 권역 합성·지도 생성 (`main.py`), 농장 3D 장면 (`farm3d.py`) |
| `services/api` | REST·WebSocket, 경보 생애주기, 민원 역추적, 시나리오 제어 |
| `orchestration/airflow_dags` | Airflow 3 DAG — 레이크하우스 5분 주기·이력 통계·Iceberg 유지 관리 + 확장 설계(농가 온보딩, 재학습) |
| `services/batch` | 레이크하우스 단계 정의 `steps.py` (Trino SQL → Iceberg) · Airflow 가 없을 때의 예비 실행기 |
| `services/weather` | Open-Meteo(JMA MSM) 과거 바람 자료 |
| `web` | 관제 화면 (React + Vite) |
| `scripts` | 원클릭 기동·정지 (Windows PowerShell) |
| `tools` | 입력 자료 만들기, 모델·jar 받기, 실행 환경 설치 |

## 모델

- **바람**: U-FNO + Fourier 특징 — 지형·건물·거칠기·풍향을 넣어 높이 6층(2~50 m) 바람장 `[u, v, w]`
- **농도**: FNO — 바람장 + 지형으로 단위 배출 농도장 (CFD 정답: 난류 확산계수 D = 2.3×10⁻⁵ + ν_t/0.7)
- 외부 시험 농가 기준 로그 농도 relL2 0.33, R² 0.70 · 시드 3개 앙상블
- 후처리: 풍향 흔들림 σθ = 3° (θ ± 3σθ 가우시안 평균) — 정상 상태 해석의 좁은 측면 폭 보완
- 한계: 중립·정상 상태 가정, 배출량은 NH₃ × 환기량 가정값, Sc_t = 0.7 민감도 미시험

## 실행 (Windows · NVIDIA GPU)

1. Docker Desktop, conda(Miniforge 등) 설치
2. 실행 환경: `powershell -File tools\setup_env.ps1` → `.conda/` (Python 3.11, PyTorch 2.6 CUDA 12.4, Node 22)
3. 화면 빌드: `cd web; ..\.conda\npm.cmd ci; ..\.conda\npm.cmd run build` → `web/dist` (API 가 이 폴더를 서빙)
4. Flink jar: `bash tools/fetch_flink_jars.sh`
5. 자료·모델 준비 (공개하지 않음 — 각자 확보)
   - `data/sources.csv`, `data/sensors.parquet`: `.conda\python.exe tools\prep_sensors.py <센서 엑셀 폴더>` (원본 센서 엑셀 → 메타·이력)
   - `data/sites/<code>/`: `.conda\python.exe tools\build_sites.py` (지형·건물·거칠기 래스터, 원자료 경로는 `DEM_SRC`·`NSDI_SHP`·`WORLDCOVER`)
   - `models/runs/...`: `MODEL_REMOTE=<host>:<path> bash tools/pull_models.sh`
   - `data/demo_config.json`: `config/demo_config.example.json` 을 복사해 채움
   - `data/basemap/`: `bash tools/fetch_basemap.sh` (오프라인 배경 지도 — 제주 PMTiles 약 10 MB, pmtiles CLI 필요)
6. 처음 한 번: `docker compose up -d` 후 `.conda\python.exe -m services.common.load_meta --history` (센서 메타·이력 → DB, Flink 가 이 표를 참조)
7. 기동: `powershell -File scripts\start_all.ps1 -Reset -Screens` → http://127.0.0.1:8000/ops , /insight

### 백업 영상 · 시연 자동 진행

`.conda\python.exe tools\demo_record.py <출력 폴더> --only=ops` (그다음 `--only=insight`)
— 시연 대본(정상 → 배출 급증 → 경보 → 3D → 저감 조치 → 해제 → 민원 → 데이터 흐름 → 가상 실험)을 자동으로 진행하며 화면을 1920×1080 15 fps 로 녹화한다 (NVENC).
한 번에 한 화면만 그려야 CPU 가 80 % 안에 든다. 녹화하는 동안 `docker compose exec -T airflow airflow dags pause odortwin_lakehouse` 로 배치를 잠시 멈추면 순간 부하가 줄어든다 (끝나면 `unpause`).

## 공개하지 않는 것

- 센서 측정값·농가 이름·좌표·입력 래스터 (`data/`), 학습된 체크포인트 (`models/`)
- 시연 설정 (`data/demo_config.json` — 실제 센서 코드)
- 실행 기록·런타임 산출물 (`logs/`, `runtime/`)

## 보안 메모

- 컨테이너 포트는 `docker-compose.yml` 에서 `127.0.0.1` 로 고정, API·엔진은 `BIND_HOST`(기본: 127.0.0.1 과 ::1) 에서만 받는다. 개발 서버(`npm run dev`)도 127.0.0.1.
- API 는 다른 사이트에서 오는 요청(CORS)·WebSocket 을 받지 않고, 화면 경로는 `web/dist` 밖의 파일을 내주지 않는다.
- `docker-compose.yml`·`infra/` 의 DB·S3 계정은 **이 PC 안에서만 쓰는 로컬 개발용 기본값**이다. 외부에 여는 환경에서는 반드시 바꾼다.
- 커밋 전 검사: `cp tools/hooks/pre-commit .git/hooks/pre-commit` — 센서 코드·농가 이름·비공개 문자열(`data/private_patterns.txt`)·자료 파일이 들어간 커밋을 막는다.

## 출처

- 배경 지도: © OpenStreetMap contributors · Protomaps (제주 범위 PMTiles 를 PC 에 저장해 오프라인으로 씀) · 바람: JMA MSM (Open-Meteo) · 지형: Copernicus GLO-30 · 토지피복: ESA WorldCover 2021 · 건물: 국토교통부 NSDI GIS 건물통합정보, Overture Maps
- FNO: Li et al., *Fourier Neural Operator for Parametric PDEs* (ICLR 2021)
