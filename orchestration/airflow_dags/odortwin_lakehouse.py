"""OdorTwin 레이크하우스 오케스트레이션 (Airflow 3).

odortwin_lakehouse          5분마다  운영 DB → bronze 증분 → silver 정제 → gold 통계(실시간 재생 구간) → 표 현황
odortwin_lakehouse_history  매일     2년 이력 기반 gold 통계 (월별 추이 · 농가 순위 · 시간대별)
odortwin_lakehouse_maint    매일     Iceberg 유지 관리 (작은 파일 합치기 · 7일 지난 스냅샷 · 고아 파일)

SQL 과 단계 정의는 services/batch/steps.py — Airflow 없이 돌릴 때의 예비 실행기(services/batch/main.py)와 같다.
작업은 컨테이너 안에서 Trino(trino:8080)와 TimescaleDB(timescaledb:5432)에 붙는다 (docker-compose.yml 의 airflow 서비스 환경 변수).
"""
from datetime import timedelta
import pendulum
from airflow.sdk import dag, task
from services.batch import steps as S

KST = pendulum.timezone('Asia/Seoul')
START = pendulum.datetime(2026, 10, 1, tz=KST)
DEFAULTS = dict(owner='odortwin', retries=2, retry_delay=timedelta(seconds=20))


def refresh_task(name):
    """silver / gold 표 하나 = 작업 하나"""
    @task(task_id=name)
    def _t():
        return S.refresh(name)
    return _t()


@dag(dag_id='odortwin_lakehouse', schedule='*/5 * * * *', start_date=START, catchup=False, max_active_runs=1,
     default_args=DEFAULTS, dagrun_timeout=timedelta(minutes=4), tags=['odortwin', 'lakehouse', 'iceberg'],
     doc_md='운영 DB(TimescaleDB) → **bronze** 증분(워터마크) → **silver** 정제·결합 → **gold** 통계. 화면(모니터 2)이 Trino 로 gold 를 읽는다.')
def lakehouse():
    @task
    def ensure_layers():
        return S.ensure_layers()

    @task
    def bronze_sensor_stream():
        return S.bronze_incremental()

    ids = list(S.SILVER) + list(S.GOLD_STREAM)

    @task(trigger_rule='all_done')                       # 앞 작업이 일부 실패해도 표 현황은 갱신
    def lake_tables(**ctx):
        rows = ctx['ti'].xcom_pull(task_ids=ids) or []
        failed = sum(1 for v in rows if v is None)
        S.lake_tables()
        S.log('silver·gold 재생성 (Airflow)', sum(int(v or 0) for v in rows), 'ok' if not failed else 'partial',
              f"DAG odortwin_lakehouse · 표 {len(ids)}개" + (f" · 실패 {failed}" if failed else ''))
        return len(ids) - failed

    layers = ensure_layers(); bronze = bronze_sensor_stream(); layers >> bronze
    silver = {n: refresh_task(n) for n in S.SILVER}
    for n, t in silver.items(): (bronze if n == 'silver.sensor_stream_clean' else layers) >> t     # bronze 를 읽는 표만 증분 뒤에
    gold = {n: refresh_task(n) for n in S.GOLD_STREAM}
    for n, t in gold.items(): silver[S.GOLD_STREAM[n][1]] >> t
    summary = lake_tables()
    for t in list(silver.values()) + list(gold.values()): t >> summary


@dag(dag_id='odortwin_lakehouse_history', schedule='@daily', start_date=START, catchup=False, max_active_runs=1,
     default_args=DEFAULTS, tags=['odortwin', 'lakehouse', 'iceberg'],
     doc_md='2023-02~2025-04 실측 이력(silver.sensor_hourly_history)에서 나오는 gold 통계. 이력은 바뀌지 않으므로 하루 1번.')
def lakehouse_history():
    @task
    def ensure_layers():
        return S.ensure_layers()

    layers = ensure_layers()
    for n in S.GOLD_HISTORY: layers >> refresh_task(n)


# 증분으로 계속 커지는 표는 파일 합치기까지, 매번 새로 만드는 표는 스냅샷만 정리
MAINT_TABLES = [('bronze.sensor_stream', True)] + [(n, False) for n in list(S.SILVER) + list(S.GOLD_STREAM) + ['gold.lake_tables']]


@dag(dag_id='odortwin_lakehouse_maint', schedule='0 3 * * *', start_date=START, catchup=False, max_active_runs=1,
     default_args=DEFAULTS, tags=['odortwin', 'lakehouse', 'iceberg', 'maintenance'],
     doc_md='Iceberg 유지 관리: 증분 표는 작은 파일 합치기(optimize), 모든 표는 7일 지난 스냅샷 만료·고아 파일 정리. 시간 여행은 최근 7일까지.')
def lakehouse_maint():
    prev = None
    for table, opt in MAINT_TABLES:                      # 차례대로 (Trino 부하를 한꺼번에 몰지 않게)
        @task(task_id=f'maint.{table}')
        def _m(table=table, opt=opt):
            return S.maintain(table, opt)
        t = _m()
        if prev is not None: prev >> t
        prev = t


lakehouse(); lakehouse_history(); lakehouse_maint()
