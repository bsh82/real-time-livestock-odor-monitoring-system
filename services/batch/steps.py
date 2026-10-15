"""레이크하우스 단계 정의 (Trino SQL → Iceberg on S3 호환 저장소, 메달리온 bronze / silver / gold).
Airflow DAG(orchestration/airflow_dags)와 예비 배치 서비스(services/batch/main.py)가 같은 단계를 쓴다.

  bronze : 원천 그대로 — sensor_history(2023-02~2025-04 이력, 초기 1회), sensor_stream(운영 DB 의 실시간 측정, 증분)
  silver : 정제·결합 — 품질 필터, 메타(농가·권역) 결합, 운영 표(배출량·경보·확산·민원) 스냅샷
  gold   : 통계 — 화면이 Trino 로 읽는 표 (STREAM: 실시간 재생 구간, HISTORY: 2년 이력)
"""
import time
from contextlib import contextmanager
import psycopg, trino
from services.common import config as C

AIRFLOW_DSN = C.PG_DSN.rsplit('/', 1)[0] + '/airflow'


def trino_con():
    return trino.dbapi.connect(host=C.TRINO_HOST, port=C.TRINO_PORT, user='odortwin-batch', catalog='lake')


def run(cur, sql):
    cur.execute(sql); rows = cur.fetchall()
    return rows[0][0] if rows and rows[0] and isinstance(rows[0][0], int) else rows


def exists(cur, schema, table):
    cur.execute(f"SELECT count(*) FROM lake.information_schema.tables WHERE table_schema='{schema}' AND table_name='{table}'")
    return cur.fetchall()[0][0] > 0


def log(job, rows, status, detail='', started=None):
    """batch_runs 에 실행 기록 (화면의 '최근 배치')"""
    with psycopg.connect(C.PG_DSN, autocommit=True) as pg:
        pg.execute('INSERT INTO batch_runs (job, started_at, finished_at, rows, status, detail) VALUES (%s, to_timestamp(%s), now(), %s, %s, %s)',
                   (job, started or time.time(), rows, status, str(detail)[:500]))


@contextmanager
def lake_lock(name, wait=True, timeout=600):
    """같은 일을 두 실행기(Airflow DAG 끼리, 또는 Airflow 와 예비 실행기)가 동시에 하지 않게 하는 PostgreSQL 권고 잠금.
    wait=False 면 이미 누가 하고 있을 때 바로 False 를 준다."""
    con = psycopg.connect(C.PG_DSN, autocommit=True)
    try:
        got, t0 = False, time.time()
        while True:
            got = con.execute('SELECT pg_try_advisory_lock(hashtext(%s))', (f'odortwin.{name}',)).fetchone()[0]
            if got or not wait or time.time() - t0 > timeout: break
            time.sleep(2)
        yield got
    finally:
        try: con.execute('SELECT pg_advisory_unlock_all()')
        finally: con.close()


def airflow_alive():
    """Airflow 스케줄러가 최근 60초 안에 살아 있었나 (메타데이터 DB 의 heartbeat)"""
    try:
        with psycopg.connect(AIRFLOW_DSN, connect_timeout=2) as con:
            return bool(con.execute("SELECT coalesce(max(latest_heartbeat) > now() - interval '60 seconds', false) FROM job WHERE job_type = 'SchedulerJob'").fetchone()[0])
    except Exception:
        return False


# ---------------- 초기화 ----------------
def ensure_layers():
    """스키마·초기 이력 표가 없으면 만든다 (있으면 바로 끝남). 두 DAG 가 동시에 처음 돌 때 겹치지 않게 잠금 안에서."""
    with lake_lock('ddl'):
        return _ensure_layers()


def _ensure_layers():
    cur = trino_con().cursor(); made = []
    for s in ('bronze', 'silver', 'gold'): run(cur, f'CREATE SCHEMA IF NOT EXISTS lake.{s}')
    if not exists(cur, 'bronze', 'sensor_history'):
        t0 = time.time()
        n = run(cur, """CREATE TABLE lake.bronze.sensor_history WITH (partitioning = ARRAY['month(ts)']) AS
                        SELECT code, ts, nh3, h2s, temp, hum, ws, wd, pm, pres FROM ts.public.sensor_history""")
        log('bronze.sensor_history (초기 적재)', n, 'ok', '2023-02~2025-04 실측 이력', t0); made.append('bronze.sensor_history')
    if not exists(cur, 'silver', 'sensor_hourly_history'):
        t0 = time.time()
        n = run(cur, """CREATE TABLE lake.silver.sensor_hourly_history WITH (partitioning = ARRAY['month(hour)']) AS
                        SELECT h.code, date_trunc('hour', h.ts) AS hour, avg(h.nh3) AS nh3_avg, max(h.nh3) AS nh3_max, avg(h.temp) AS temp_avg, count(*) AS n,
                               s.farm, s.role, s.cluster, s.place
                        FROM lake.bronze.sensor_history h JOIN ts.public.sources s ON h.code = s.code
                        WHERE h.nh3 BETWEEN 0 AND 100 GROUP BY 1, 2, s.farm, s.role, s.cluster, s.place""")
        log('silver.sensor_hourly_history', n, 'ok', '시간 집계·품질 필터(0~100 ppm)', t0); made.append('silver.sensor_hourly_history')
    if not exists(cur, 'bronze', 'sensor_stream'):
        run(cur, """CREATE TABLE lake.bronze.sensor_stream (code varchar, ts timestamp(6) with time zone, nh3 real, h2s real, temp real, hum real,
                    ws real, wd real, pm real, pres real, qc varchar, ingest_at timestamp(6) with time zone) WITH (partitioning = ARRAY['day(ts)'])""")
        made.append('bronze.sensor_stream')
    return made


# ---------------- bronze 증분 ----------------
def bronze_incremental():
    """운영 DB(TimescaleDB sensor_raw) 의 새 측정 → bronze.sensor_stream (ingest_at 워터마크 이후만).
    워터마크 읽기~적재를 잠금 안에서 → 두 실행기가 같은 구간을 두 번 넣지 않는다 (이미 누가 하고 있으면 건너뜀)."""
    with lake_lock('bronze', wait=False) as got:
        if not got: return 0
        return _bronze_incremental()


def _bronze_incremental():
    cur = trino_con().cursor(); t0 = time.time()
    cur.execute('SELECT max(ingest_at) FROM lake.bronze.sensor_stream'); wm = cur.fetchall()[0][0]
    cond = f"WHERE ingest_at > TIMESTAMP '{wm.isoformat(sep=' ')}'" if wm else ''
    n = int(run(cur, f'INSERT INTO lake.bronze.sensor_stream SELECT code, ts, nh3, h2s, temp, hum, ws, wd, pm, pres, qc, ingest_at FROM ts.public.sensor_raw {cond}') or 0)
    log('bronze.sensor_stream (증분)', n, 'ok', f'watermark {wm}', t0)
    return n


# ---------------- silver / gold 표 ----------------
SILVER = {
    'silver.sensor_stream_clean': """CREATE OR REPLACE TABLE lake.silver.sensor_stream_clean AS
        SELECT b.code, b.ts, b.nh3, b.temp, b.hum, s.farm, s.role, s.cluster FROM lake.bronze.sensor_stream b
        JOIN ts.public.sources s ON b.code = s.code WHERE b.qc = 'OK'""",
    'silver.emission': 'CREATE OR REPLACE TABLE lake.silver.emission AS SELECT e.*, s.farm, s.cluster FROM ts.public.emission e JOIN ts.public.sources s ON e.code = s.code',
    'silver.alarms': 'CREATE OR REPLACE TABLE lake.silver.alarms AS SELECT * FROM ts.public.alarms',
    'silver.dispersion': 'CREATE OR REPLACE TABLE lake.silver.dispersion AS SELECT * FROM ts.public.dispersion',
    'silver.complaints': "CREATE OR REPLACE TABLE lake.silver.complaints AS SELECT id, ts, lat, lon, place, intensity, status, json_format(attribution) AS attribution FROM ts.public.complaints",
}
# 실시간 재생 구간에서 나오는 gold (5분마다): 이름 → (SQL, 앞선 silver)
GOLD_STREAM = {
    'gold.cluster_exposure': ("""CREATE OR REPLACE TABLE lake.gold.cluster_exposure AS
        SELECT cluster, round(count_if(homes_06 > 0) / 6.0, 1) AS exposure_hours, max(homes_06) AS max_homes, round(max(cmax_ppm), 2) AS cmax_ppm,
               round(avg(q_total), 4) AS q_avg_gs, count(*) AS slots FROM lake.silver.dispersion GROUP BY cluster""", 'silver.dispersion'),
    'gold.alarm_summary': ("""CREATE OR REPLACE TABLE lake.gold.alarm_summary AS
        SELECT kind, level, count(*) AS n, count_if(state = 'cleared') AS cleared, count_if(action IS NOT NULL) AS actions
        FROM lake.silver.alarms GROUP BY kind, level""", 'silver.alarms'),
    'gold.complaint_summary': ("""CREATE OR REPLACE TABLE lake.gold.complaint_summary AS
        SELECT coalesce(json_extract_scalar(attribution, '$.sources[0].farm'), '(미상)') AS top_farm, count(*) AS n, round(avg(intensity), 1) AS intensity
        FROM lake.silver.complaints GROUP BY 1""", 'silver.complaints'),
    'gold.daily_stream': ("""CREATE OR REPLACE TABLE lake.gold.daily_stream AS
        SELECT date_trunc('day', e.ts) AS day, count(*) AS readings, round(avg(e.nh3), 3) AS nh3_avg, round(sum(e.q_gs) * 60 / 1000, 2) AS emission_kg,
               count(DISTINCT e.code) AS sources FROM lake.silver.emission e GROUP BY 1""", 'silver.emission'),
}
# 2년 이력에서 나오는 gold (하루 1번이면 충분)
GOLD_HISTORY = {
    'gold.monthly_trend': """CREATE OR REPLACE TABLE lake.gold.monthly_trend AS
        SELECT date_trunc('month', hour) AS month, round(avg(nh3_avg), 3) AS nh3_avg, round(approx_percentile(nh3_avg, 0.95), 3) AS nh3_p95,
               count(DISTINCT code) AS sensors, round(count_if(nh3_avg > 5) / 1.0, 0) AS hours_over_5ppm
        FROM lake.silver.sensor_hourly_history WHERE role <> 'measure' GROUP BY 1""",
    'gold.farm_rank': """CREATE OR REPLACE TABLE lake.gold.farm_rank AS
        SELECT row_number() OVER (ORDER BY avg(nh3_avg) DESC) AS rank, farm, count(DISTINCT code) AS sensors, round(avg(nh3_avg), 3) AS nh3_avg,
               round(max(nh3_max), 2) AS nh3_max, count_if(nh3_avg > 5) AS hours_over_5ppm, count(*) AS data_hours
        FROM lake.silver.sensor_hourly_history WHERE role <> 'measure' AND nh3_max < 100 GROUP BY farm""",
    'gold.hourly_profile': """CREATE OR REPLACE TABLE lake.gold.hourly_profile AS
        SELECT hour(hour) AS hour, round(avg(nh3_avg), 3) AS nh3_avg, round(avg(temp_avg), 2) AS temp_avg
        FROM lake.silver.sensor_hourly_history WHERE role <> 'measure' GROUP BY 1""",
}
ALL = {**SILVER, **{k: v[0] for k, v in GOLD_STREAM.items()}, **GOLD_HISTORY}


def refresh(name, log_error=True):
    """표 하나를 다시 만든다. 실패하면 예외를 다시 던진다 (Airflow 가 재시도). batch_runs 오류 기록은 마지막 시도에서만(log_error)"""
    cur = trino_con().cursor(); t0 = time.time()
    try: return int(run(cur, ALL[name]) or 0)
    except Exception as e:
        if log_error: log(name, 0, 'error', str(e), t0)
        raise


LAKE_TABLES = [('bronze', 'sensor_history'), ('bronze', 'sensor_stream'), ('silver', 'sensor_hourly_history'), ('silver', 'sensor_stream_clean'),
               ('silver', 'emission'), ('silver', 'dispersion'), ('silver', 'alarms'), ('gold', 'monthly_trend'), ('gold', 'farm_rank'),
               ('gold', 'hourly_profile'), ('gold', 'cluster_exposure'), ('gold', 'alarm_summary'), ('gold', 'daily_stream'),
               ('silver', 'complaints'), ('gold', 'complaint_summary')]


def lake_tables():
    """계층별 표·행 수 요약 → gold.lake_tables (화면의 bronze/silver/gold 행 수)"""
    cur = trino_con().cursor()
    union = ' UNION ALL '.join(f"SELECT '{l}' AS layer, '{t}' AS table_name, count(*) AS row_count FROM lake.{l}.{t}" for l, t in LAKE_TABLES if exists(cur, l, t))
    run(cur, f'CREATE OR REPLACE TABLE lake.gold.lake_tables AS {union}')
    return len(LAKE_TABLES)


# ---------------- Iceberg 유지 관리 ----------------
def maintain(table, optimize=False, retention='1d'):
    """작은 파일 합치기(optimize) · 오래된 스냅샷 지우기 · 고아 파일 정리. 없는 표는 건너뜀.
    5분마다 새로 만드는 표는 스냅샷이 하루 288개씩 쌓이므로 1일만 남기고, 증분 표(bronze)는 7일(시간 여행)."""
    cur = trino_con().cursor(); done = []
    schema, name = table.split('.')
    if not exists(cur, schema, name): return ['skip (없음)']
    if optimize: run(cur, f"ALTER TABLE lake.{table} EXECUTE optimize(file_size_threshold => '64MB')"); done.append('optimize')
    run(cur, f"ALTER TABLE lake.{table} EXECUTE expire_snapshots(retention_threshold => '{retention}')"); done.append('expire_snapshots')
    run(cur, f"ALTER TABLE lake.{table} EXECUTE remove_orphan_files(retention_threshold => '{retention}')"); done.append('remove_orphan_files')
    return done
