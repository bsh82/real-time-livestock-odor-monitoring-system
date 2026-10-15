"""OdorTwin 확장 설계 DAG — 운영 단계에서 오케스트레이션이 필요한 작업의 구조 (실행 연동 전).

여기 있는 DAG 는 **설계도**다: 각 작업은 아직 실제 처리를 하지 않는 빈 작업(EmptyOperator)이고, 일정 없이 일시정지 상태로 만들어진다.
작업 설명(doc)에 지금 그 일을 수동으로 하는 도구를 적어 두었다 — 운영 전환 때 이 도구들을 작업으로 옮긴다.

odortwin_farm_onboarding  신규 농가 → 지형·건물·토지피복 수집 → 2 km 입력 래스터 → 권역·수용체 재구성 → 엔진 반영
odortwin_model_retrain    CFD 정답 생성 → 학습 자료 → 바람·농도 재학습 → 외부 시험 검증 → 기준 통과 시에만 배포
"""
import pendulum
from airflow.sdk import DAG
from airflow.providers.standard.operators.empty import EmptyOperator

START = pendulum.datetime(2026, 10, 1, tz=pendulum.timezone('Asia/Seoul'))
COMMON = dict(schedule=None, start_date=START, catchup=False, is_paused_upon_creation=True, tags=['odortwin', '설계', '실행 연동 전'])


def step(task_id, doc):
    return EmptyOperator(task_id=task_id, doc_md=doc)


with DAG('odortwin_farm_onboarding', description='[설계 · 실행 연동 전] 신규 농가 온보딩', **COMMON,
         doc_md='신규 농가를 관제 대상에 넣는 과정. 지금은 tools/prep_sensors.py · tools/build_sites.py 를 손으로 실행한다.') as onboarding:
    meta = step('register_sensor_meta', '센서 메타(위치·시설 종류·역할) 등록 → TimescaleDB sources. 현재: tools/prep_sensors.py')
    dem = step('fetch_terrain_dsm', 'Copernicus GLO-30 DSM 타일 → 5 m 재표본. 현재: tools/build_sites.py (DEM_SRC)')
    bld = step('fetch_buildings', 'NSDI 건물통합정보 + Overture Maps 보충 → 높이 규칙. 현재: tools/build_sites.py (NSDI_SHP, overturemaps)')
    lc = step('fetch_landcover_z0', 'ESA WorldCover → 거칠기 z0. 현재: tools/build_sites.py (WORLDCOVER)')
    raster = step('build_site_rasters', '2 km × 2 km 입력 래스터(terrain·building_height·z0) + site.json')
    clus = step('rebuild_clusters', '배출원 위치로 권역(클러스터) 재구성 — 엔진 시작 때 계산')
    rec = step('recompute_receptors', '권역 안 주택·학교·노유자시설 수용체 재계산')
    reload = step('reload_engine', '확산 엔진 재시작 → 새 농가 입력을 GPU 캐시에 적재')
    check = step('verify_farm3d', '/api/farm3d 로 새 농가 3D 장면·확산 확인')
    meta >> [dem, bld, lc] >> raster >> clus >> rec >> reload >> check

with DAG('odortwin_model_retrain', description='[설계 · 실행 연동 전] CFD 정답 → 재학습 → 검증 → 배포', **COMMON,
         doc_md='확산계수·지형 자료가 바뀌거나 실측 대비 오차가 커졌을 때의 재학습 과정. 2026-10-14 확산계수 변경(D = ν_t/0.7) 때 이 과정을 손으로 했다.') as retrain:
    cfd = step('generate_cfd_truth', 'OpenFOAM RANS 바람장 + 농도 수송 — 농가 × 36 풍향 (풍향당 약 35~45분, 원격 계산 서버)')
    data = step('collect_dataset', 'CFD 결과 → 학습 자료 (입력 래스터·바람·농도 6층)')
    wind = step('train_wind_ufno', 'U-FNO+F 바람 모델 재학습 (시드 3개)')
    conc = step('train_conc_fno', 'FNO 농도 모델 재학습 (시드 3개)')
    ev = step('evaluate_external', '외부 시험 농가 검증: 로그 농도 relL2 · R² (현재 기준 0.33 · 0.70)')
    gate = step('quality_gate', '기준보다 나아졌을 때만 다음 단계로 (운영 전환 때 분기 작업으로 바꿈)')
    deploy = step('deploy_checkpoints', '체크포인트 → models/runs (현재: tools/pull_models.sh)')
    restart = step('restart_engine', '엔진 재시작 · 화면 출처 문구의 성능 수치 갱신')
    cfd >> data >> [wind, conc] >> ev >> gate >> deploy >> restart
