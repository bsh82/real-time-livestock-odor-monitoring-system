"""OdorTwin 레이크하우스 오케스트레이션 (Airflow 3).

odortwin_lakehouse          5분마다  운영 DB → bronze 증분 → silver 정제 → gold 통계(실시간 재생 구간) → 표 현황
odortwin_lakehouse_history  매일     2년 이력 기반 gold 통계 (월별 추이 · 농가 순위 · 시간대별)
odortwin_lakehouse_maint    매일     Iceberg 유지 관리 (작은 파일 합치기 · 지난 스냅샷 · 고아 파일) + 오래된 작업 로그 정리

SQL 과 단계 정의는 services/batch/steps.py — Airflow 없이 돌릴 때의 예비 실행기(services/batch/main.py)와 같다.
두 실행기가 같은 일을 동시에 하지 않도록 bronze 적재·초기화는 PostgreSQL 권고 잠금 안에서 한다 (steps.lake_lock).
작업은 컨테이너 안에서 Trino(trino:8080)와 TimescaleDB(timescaledb:5432)에 붙는다 (docker-compose.yml 의 airflow 서비스 환경 변수).
"""
import os, time
from datetime import timedelta
import pendulum
from airflow.sdk import dag, task, get_current_context
from airflow.sdk.exceptions import AirflowFailException
from services.batch import steps as S

KST = pendulum.timezone('Asia/Seoul')
START = pendulum.datetime(2026, 10, 1, tz=KST)
DEFAULTS = dict(owner='odortwin', retries=2, retry_delay=timedelta(seconds=20))


def refresh_task(name):
    """silver / gold 표 하나 = 작업 하나 (batch_runs 오류 기록은 마지막 재시도에서만)"""
    @task(task_id=name)
    def _t():
        ti = get_current_context()['ti']
        return S.refresh(name, log_error=ti.try_number > ti.max_tries)
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

    @task(trigger_rule='all_done', retries=0)            # 앞 작업이 일부 실패해도 표 현황·기록은 남기고, 실패가 있으면 실행 전체를 실패로
    def lake_tables():
        ctx = get_current_context()
        rows = ctx['ti'].xcom_pull(task_ids=ids) or []
        failed = sum(1 for v in rows if v is None)
        S.lake_tables()
        started = ctx['dag_run'].start_date.timestamp() if ctx['dag_run'].start_date else None
        S.log('silver·gold 재생성 (Airflow)', sum(int(v or 0) for v in rows), 'ok' if not failed else 'partial',
              f"DAG odortwin_lakehouse · 표 {len(ids)}개" + (f" · 실패 {failed}" if failed else ''), started)
        if failed: raise AirflowFailException(f'{failed}/{len(ids)} 표 갱신 실패')
        return len(ids)

    layers = ensure_layers(); bronze = bronze_sensor_stream(); layers >> bronze
    silver = {n: refresh_task(n) for n in S.SILVER}
    for n, t in silver.items(): (bronze if n == 'silver.sensor_stream_clean' else layers) >> t     # bronze 를 읽는 표만 증분 뒤에
    gold = {n: refresh_task(n) for n in S.GOLD_STREAM}
    for n, t in gold.items(): silver[S.GOLD_STREAM[n][1]] >> t
    summary = lake_tables()
    for t in list(silver.values()) + list(gold.values()): t >> summary


@dag(dag_id='odortwin_lakehouse_history', schedule='@daily', start_date=START, catchup=False, max_active_runs=1,
     default_args=DEFAULTS, dagrun_timeout=timedelta(minutes=20), tags=['odortwin', 'lakehouse', 'iceberg'],
     doc_md='2023-02~2025-04 실측 이력(silver.sensor_hourly_history)에서 나오는 gold 통계. 이력은 바뀌지 않으므로 하루 1번.')
def lakehouse_history():
    @task
    def ensure_layers():
        return S.ensure_layers()

    layers = ensure_layers()
    for n in S.GOLD_HISTORY: layers >> refresh_task(n)


# 증분으로 계속 커지는 표는 파일 합치기까지 + 스냅샷 7일(시간 여행), 5분마다 새로 만드는 표는 스냅샷 1일만
MAINT_TABLES = [('bronze.sensor_stream', True, '7d')] + [(n, False, '1d') for n in list(S.SILVER) + list(S.GOLD_STREAM) + ['gold.lake_tables']]
LOG_DAYS = 3                                              # Airflow 작업 로그 보존 (일)


@dag(dag_id='odortwin_lakehouse_maint', schedule='0 3 * * *', start_date=START, catchup=False, max_active_runs=1,
     default_args=DEFAULTS, dagrun_timeout=timedelta(minutes=40), tags=['odortwin', 'lakehouse', 'iceberg', 'maintenance'],
     doc_md='Iceberg 유지 관리: 증분 표는 작은 파일 합치기(optimize)·스냅샷 7일, 매번 새로 만드는 표는 스냅샷 1일·고아 파일 정리. 끝에 오래된 작업 로그 정리.')
def lakehouse_maint():
    prev = None
    for table, opt, keep in MAINT_TABLES:                # 차례대로 (Trino 부하를 한꺼번에 몰지 않게), 앞 표가 실패해도 다음 표는 진행
        @task(task_id=f'maint.{table}', trigger_rule='all_done')
        def _m(table=table, opt=opt, keep=keep):
            return S.maintain(table, opt, keep)
        t = _m()
        if prev is not None: prev >> t
        prev = t

    @task(trigger_rule='all_done')
    def cleanup_task_logs():
        """LOG_DAYS 일보다 오래된 작업 로그 파일 삭제 (전시 PC 디스크 보호)"""
        root = os.environ.get('AIRFLOW_HOME', '/opt/airflow') + '/logs'; cut = time.time() - LOG_DAYS * 86400; n = 0
        for d, _, files in os.walk(root):
            for f in files:
                p = os.path.join(d, f)
                try:
                    if os.path.getmtime(p) < cut: os.remove(p); n += 1
                except OSError: pass
        return n

    prev >> cleanup_task_logs()


lakehouse(); lakehouse_history(); lakehouse_maint()
