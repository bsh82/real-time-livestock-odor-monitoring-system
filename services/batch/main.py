"""레이크하우스 배치 (Trino SQL → Iceberg on S3 호환 저장소, 메달리온 Bronze/Silver/Gold).
초기 1회: 2023-02~2025-04 전체 이력 310만 건 → bronze.sensor_history (월 파티션) → silver.sensor_hourly_history
주기(기본 120초): 운영 DB(TimescaleDB)의 새 데이터 → bronze.sensor_stream → silver.* → gold.* 재생성, batch_runs 에 기록
실행: python -m services.batch.main [--once]
"""
import os, sys, time, traceback
import psycopg, trino
from services.common import config as C

INTERVAL = int(os.getenv('BATCH_INTERVAL', 120))


def trino_con():
    return trino.dbapi.connect(host=C.TRINO_HOST, port=C.TRINO_PORT, user='odortwin-batch', catalog='lake')


def run(cur, sql):
    cur.execute(sql); rows = cur.fetchall()
    return rows[0][0] if rows and rows[0] and isinstance(rows[0][0], int) else rows


def exists(cur, schema, table):
    cur.execute(f"SELECT count(*) FROM lake.information_schema.tables WHERE table_schema='{schema}' AND table_name='{table}'")
    return cur.fetchall()[0][0] > 0


def log(job, rows, status, detail='', started=None):
    with psycopg.connect(C.PG_DSN, autocommit=True) as pg:
        pg.execute('INSERT INTO batch_runs (job, started_at, finished_at, rows, status, detail) VALUES (%s, to_timestamp(%s), now(), %s, %s, %s)',
                   (job, started or time.time(), rows, status, detail[:500]))


def init(cur):
    for s in ('bronze', 'silver', 'gold'): run(cur, f'CREATE SCHEMA IF NOT EXISTS lake.{s}')
    if not exists(cur, 'bronze', 'sensor_history'):
        t0 = time.time()
        n = run(cur, """CREATE TABLE lake.bronze.sensor_history WITH (partitioning = ARRAY['month(ts)']) AS
                        SELECT code, ts, nh3, h2s, temp, hum, ws, wd, pm, pres FROM ts.public.sensor_history""")
        log('bronze.sensor_history (초기 적재)', n, 'ok', '2023-02~2025-04 실측 이력', t0); print('bronze.sensor_history', n, flush=True)
    if not exists(cur, 'silver', 'sensor_hourly_history'):
        t0 = time.time()
        n = run(cur, """CREATE TABLE lake.silver.sensor_hourly_history WITH (partitioning = ARRAY['month(hour)']) AS
                        SELECT h.code, date_trunc('hour', h.ts) AS hour, avg(h.nh3) AS nh3_avg, max(h.nh3) AS nh3_max, avg(h.temp) AS temp_avg, count(*) AS n,
                               s.farm, s.role, s.cluster, s.place
                        FROM lake.bronze.sensor_history h JOIN ts.public.sources s ON h.code = s.code
                        WHERE h.nh3 BETWEEN 0 AND 100 GROUP BY 1, 2, s.farm, s.role, s.cluster, s.place""")
        log('silver.sensor_hourly_history', n, 'ok', '시간 집계·품질 필터(0~100 ppm)', t0); print('silver.sensor_hourly_history', n, flush=True)
    if not exists(cur, 'bronze', 'sensor_stream'):
        run(cur, """CREATE TABLE lake.bronze.sensor_stream (code varchar, ts timestamp(6) with time zone, nh3 real, h2s real, temp real, hum real,
                    ws real, wd real, pm real, pres real, qc varchar, ingest_at timestamp(6) with time zone) WITH (partitioning = ARRAY['day(ts)'])""")


def incremental(cur):
    t0 = time.time()
    cur.execute('SELECT max(ingest_at) FROM lake.bronze.sensor_stream'); wm = cur.fetchall()[0][0]
    cond = f"WHERE ingest_at > TIMESTAMP '{wm.isoformat(sep=' ')}'" if wm else ''
    n = run(cur, f'INSERT INTO lake.bronze.sensor_stream SELECT code, ts, nh3, h2s, temp, hum, ws, wd, pm, pres, qc, ingest_at FROM ts.public.sensor_raw {cond}')
    log('bronze.sensor_stream (증분)', n, 'ok', f'watermark {wm}', t0)
    t0 = time.time()
    steps = {
        'silver.sensor_stream_clean': """CREATE OR REPLACE TABLE lake.silver.sensor_stream_clean AS
            SELECT b.code, b.ts, b.nh3, b.temp, b.hum, s.farm, s.role, s.cluster FROM lake.bronze.sensor_stream b
            JOIN ts.public.sources s ON b.code = s.code WHERE b.qc = 'OK'""",
        'silver.emission': 'CREATE OR REPLACE TABLE lake.silver.emission AS SELECT e.*, s.farm, s.cluster FROM ts.public.emission e JOIN ts.public.sources s ON e.code = s.code',
        'silver.alarms': 'CREATE OR REPLACE TABLE lake.silver.alarms AS SELECT * FROM ts.public.alarms',
        'silver.dispersion': 'CREATE OR REPLACE TABLE lake.silver.dispersion AS SELECT * FROM ts.public.dispersion',
        'silver.complaints': "CREATE OR REPLACE TABLE lake.silver.complaints AS SELECT id, ts, lat, lon, place, intensity, status, json_format(attribution) AS attribution FROM ts.public.complaints",
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
        'gold.cluster_exposure': """CREATE OR REPLACE TABLE lake.gold.cluster_exposure AS
            SELECT cluster, round(count_if(homes_06 > 0) / 6.0, 1) AS exposure_hours, max(homes_06) AS max_homes, round(max(cmax_ppm), 2) AS cmax_ppm,
                   round(avg(q_total), 4) AS q_avg_gs, count(*) AS slots FROM lake.silver.dispersion GROUP BY cluster""",
        'gold.alarm_summary': """CREATE OR REPLACE TABLE lake.gold.alarm_summary AS
            SELECT kind, level, count(*) AS n, count_if(state = 'cleared') AS cleared, count_if(action IS NOT NULL) AS actions
            FROM lake.silver.alarms GROUP BY kind, level""",
        'gold.complaint_summary': """CREATE OR REPLACE TABLE lake.gold.complaint_summary AS
            SELECT coalesce(json_extract_scalar(attribution, '$.sources[0].farm'), '(미상)') AS top_farm, count(*) AS n, round(avg(intensity), 1) AS intensity
            FROM lake.silver.complaints GROUP BY 1""",
        'gold.daily_stream': """CREATE OR REPLACE TABLE lake.gold.daily_stream AS
            SELECT date_trunc('day', e.ts) AS day, count(*) AS readings, round(avg(e.nh3), 3) AS nh3_avg, round(sum(e.q_gs) * 60 / 1000, 2) AS emission_kg,
                   count(DISTINCT e.code) AS sources FROM lake.silver.emission e GROUP BY 1""",
    }
    total = 0
    for name, sql in steps.items():
        try: total += int(run(cur, sql) or 0)
        except Exception as e: log(name, 0, 'error', str(e)); print('step error', name, str(e)[:200], flush=True)
    tables = [('bronze', 'sensor_history'), ('bronze', 'sensor_stream'), ('silver', 'sensor_hourly_history'), ('silver', 'sensor_stream_clean'),
              ('silver', 'emission'), ('silver', 'dispersion'), ('silver', 'alarms'), ('gold', 'monthly_trend'), ('gold', 'farm_rank'),
              ('gold', 'hourly_profile'), ('gold', 'cluster_exposure'), ('gold', 'alarm_summary'), ('gold', 'daily_stream')]
    union = ' UNION ALL '.join(f"SELECT '{l}' AS layer, '{t}' AS table_name, count(*) AS row_count FROM lake.{l}.{t}" for l, t in tables if exists(cur, l, t))
    run(cur, f'CREATE OR REPLACE TABLE lake.gold.lake_tables AS {union}')
    log('silver·gold 재생성', total, 'ok', f'{len(steps)}개 테이블', t0)
    print(f'batch ok: bronze +{n}, silver/gold {total} rows, {time.time() - t0:.1f}s', flush=True)


def main(once):
    cur = trino_con().cursor(); init(cur)
    while True:
        try: incremental(cur)
        except Exception as e:
            traceback.print_exc(); log('batch', 0, 'error', str(e)); cur = trino_con().cursor()
        if once: break
        time.sleep(INTERVAL)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8'); main('--once' in sys.argv)
