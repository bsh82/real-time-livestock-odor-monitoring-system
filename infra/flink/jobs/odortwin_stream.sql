-- OdorTwin 실시간 처리 (Flink SQL). 실행: docker compose exec flink-jobmanager ./bin/sql-client.sh -f /opt/flink/jobs/odortwin_stream.sql
-- odor.raw ─┬─ 정제·품질 플래그 ─→ odor.clean (Kafka), sensor_raw (TimescaleDB)
--           ├─ 10분 집계(TUMBLE) ─→ sensor_agg10m
--           ├─ 배출량 = NH₃ × 환기량(기온 보정) ─→ odor.emission, emission
--           └─ 경보: 5분 연속 기준 초과(MATCH_RECOGNIZE), 측정 범위 이탈, 4시간 고착값 ─→ odor.alert, alert_events
SET 'pipeline.name' = 'odortwin-stream';
SET 'table.exec.source.idle-timeout' = '5 s';
SET 'execution.checkpointing.interval' = '10 s';

CREATE TABLE odor_raw (
  code STRING, ts_ms BIGINT, seq BIGINT, sent_ms BIGINT, ingest_ms BIGINT,
  nh3 DOUBLE, h2s DOUBLE, temp DOUBLE, hum DOUBLE, ws DOUBLE, wd DOUBLE, pm DOUBLE, pres DOUBLE,
  ts AS TO_TIMESTAMP_LTZ(ts_ms, 3),
  proc_time AS PROCTIME(),
  WATERMARK FOR ts AS ts - INTERVAL '30' SECOND
) WITH (
  'connector' = 'kafka', 'topic' = 'odor.raw', 'properties.bootstrap.servers' = 'kafka:29092',
  'properties.group.id' = 'flink-odortwin', 'scan.startup.mode' = 'latest-offset',
  'format' = 'json', 'json.ignore-parse-errors' = 'true'
);

CREATE TABLE dim_sources (
  code STRING, farm STRING, role STRING, place STRING, cluster STRING, vent_base DOUBLE, nh3_p95 FLOAT,
  PRIMARY KEY (code) NOT ENFORCED
) WITH (
  'connector' = 'jdbc', 'url' = 'jdbc:postgresql://timescaledb:5432/odor', 'table-name' = 'sources',
  'username' = 'odor', 'password' = 'odortwin',
  'lookup.cache' = 'PARTIAL', 'lookup.partial-cache.max-rows' = '1000', 'lookup.partial-cache.expire-after-write' = '10 min'
);

-- 정제: 메타 결합(lookup join) + 범위 검사 + 경보 기준(배출원: 평소 상위 5 % × 1.2, 최소 3 ppm / 측정 지점: 2 ppm)
CREATE TEMPORARY VIEW clean AS
SELECT r.code, r.ts, r.ts_ms, r.sent_ms, r.ingest_ms, r.nh3, r.h2s, r.temp, r.hum, r.ws, r.wd, r.pm, r.pres,
       CASE WHEN r.nh3 < 0 OR r.nh3 > 100 THEN 'RANGE' ELSE 'OK' END AS qc,
       d.role, d.place, d.cluster, COALESCE(d.vent_base, 20.0) AS vent_base,
       CASE WHEN d.role = 'measure' THEN 2.0 ELSE GREATEST(COALESCE(CAST(d.nh3_p95 AS DOUBLE), 3.0) * 1.2, 3.0) END AS thr
FROM odor_raw AS r
JOIN dim_sources FOR SYSTEM_TIME AS OF r.proc_time AS d ON r.code = d.code;

CREATE TABLE odor_clean_out (
  code STRING, ts_ms BIGINT, sent_ms BIGINT, ingest_ms BIGINT, nh3 DOUBLE, h2s DOUBLE, temp DOUBLE, hum DOUBLE,
  ws DOUBLE, wd DOUBLE, qc STRING, role STRING, cluster STRING, thr DOUBLE
) WITH (
  'connector' = 'kafka', 'topic' = 'odor.clean', 'properties.bootstrap.servers' = 'kafka:29092',
  'key.format' = 'raw', 'key.fields' = 'code', 'value.format' = 'json'
);

CREATE TABLE sensor_raw_out (
  code STRING, ts TIMESTAMP(3), nh3 FLOAT, h2s FLOAT, temp FLOAT, hum FLOAT, ws FLOAT, wd FLOAT, pm FLOAT, pres FLOAT, qc STRING
) WITH (
  'connector' = 'jdbc', 'url' = 'jdbc:postgresql://timescaledb:5432/odor', 'table-name' = 'sensor_raw',
  'username' = 'odor', 'password' = 'odortwin', 'sink.buffer-flush.interval' = '1s', 'sink.buffer-flush.max-rows' = '500'
);

CREATE TABLE agg10m_out (
  code STRING, window_start TIMESTAMP(3), window_end TIMESTAMP(3), nh3_avg FLOAT, nh3_max FLOAT, temp_avg FLOAT, n BIGINT,
  PRIMARY KEY (code, window_start) NOT ENFORCED
) WITH (
  'connector' = 'jdbc', 'url' = 'jdbc:postgresql://timescaledb:5432/odor', 'table-name' = 'sensor_agg10m',
  'username' = 'odor', 'password' = 'odortwin', 'sink.buffer-flush.interval' = '2s'
);

CREATE TABLE emission_out (
  code STRING, ts_ms BIGINT, cluster STRING, nh3 DOUBLE, temp DOUBLE, vent DOUBLE, q_gs DOUBLE
) WITH (
  'connector' = 'kafka', 'topic' = 'odor.emission', 'properties.bootstrap.servers' = 'kafka:29092',
  'key.format' = 'raw', 'key.fields' = 'code', 'value.format' = 'json'
);

CREATE TABLE emission_db (
  code STRING, ts TIMESTAMP(3), q_gs DOUBLE, nh3 FLOAT, temp FLOAT, vent DOUBLE, PRIMARY KEY (code, ts) NOT ENFORCED
) WITH (
  'connector' = 'jdbc', 'url' = 'jdbc:postgresql://timescaledb:5432/odor', 'table-name' = 'emission',
  'username' = 'odor', 'password' = 'odortwin', 'sink.buffer-flush.interval' = '2s'
);

CREATE TABLE alert_out (
  code STRING, ts_ms BIGINT, kind STRING, level STRING, `value` DOUBLE, thr DOUBLE, cluster STRING, detail STRING
) WITH (
  'connector' = 'kafka', 'topic' = 'odor.alert', 'properties.bootstrap.servers' = 'kafka:29092',
  'key.format' = 'raw', 'key.fields' = 'code', 'value.format' = 'json'
);

CREATE TABLE alert_db (
  code STRING, ts TIMESTAMP(3), kind STRING, level STRING, `value` FLOAT, detail STRING
) WITH (
  'connector' = 'jdbc', 'url' = 'jdbc:postgresql://timescaledb:5432/odor', 'table-name' = 'alert_events',
  'username' = 'odor', 'password' = 'odortwin', 'sink.buffer-flush.interval' = '1s'
);

CREATE TEMPORARY VIEW emission AS
SELECT code, ts, ts_ms, cluster, nh3, temp,
       vent_base * GREATEST(0.4, LEAST(1.0, 0.4 + 0.6 * (temp - 5.0) / 25.0)) AS vent
FROM clean WHERE role <> 'measure' AND qc = 'OK';

CREATE TEMPORARY VIEW alerts AS
SELECT code, ts_ms, CAST('EMISSION_HIGH' AS STRING) AS kind, CAST('caution' AS STRING) AS level, peak AS `value`, thr, cluster,
       CONCAT('NH3 ', CAST(CAST(peak AS DECIMAL(6, 1)) AS STRING), ' ppm, 5분 연속 기준(', CAST(CAST(thr AS DECIMAL(5, 1)) AS STRING), ' ppm) 초과') AS detail
FROM clean
MATCH_RECOGNIZE (
  PARTITION BY code ORDER BY ts
  MEASURES LAST(A.ts_ms) AS ts_ms, MAX(A.nh3) AS peak, LAST(A.thr) AS thr, LAST(A.cluster) AS cluster
  ONE ROW PER MATCH
  AFTER MATCH SKIP PAST LAST ROW
  PATTERN (A{5})
  DEFINE A AS A.nh3 > A.thr AND A.qc = 'OK'
)
UNION ALL
SELECT code, ts_ms, CAST('SENSOR_RANGE' AS STRING), CAST('fault' AS STRING), nh3, CAST(100.0 AS DOUBLE), cluster, CONCAT('측정 범위 이탈 NH3 ', CAST(CAST(nh3 AS DECIMAL(8, 1)) AS STRING), ' ppm')
FROM clean WHERE qc = 'RANGE'
UNION ALL
SELECT code, ts_ms, CAST('SENSOR_STUCK' AS STRING), CAST('fault' AS STRING), nh3, CAST(0.0 AS DOUBLE), cluster, CAST('4시간 같은 값 반복 (고착 의심)' AS STRING)
FROM (
  SELECT code, ts_ms, nh3, cluster,
         MAX(nh3) OVER w - MIN(nh3) OVER w AS rng, COUNT(*) OVER w AS n
  FROM clean
  WINDOW w AS (PARTITION BY code ORDER BY ts RANGE BETWEEN INTERVAL '4' HOUR PRECEDING AND CURRENT ROW)
) WHERE rng = 0 AND n >= 230 AND nh3 > 0.5 AND MOD(n, 60) = 50;

EXECUTE STATEMENT SET
BEGIN
  INSERT INTO odor_clean_out SELECT code, ts_ms, sent_ms, ingest_ms, nh3, h2s, temp, hum, ws, wd, qc, role, cluster, thr FROM clean;
  INSERT INTO sensor_raw_out SELECT code, CAST(ts AS TIMESTAMP(3)), CAST(nh3 AS FLOAT), CAST(h2s AS FLOAT), CAST(temp AS FLOAT), CAST(hum AS FLOAT),
                                    CAST(ws AS FLOAT), CAST(wd AS FLOAT), CAST(pm AS FLOAT), CAST(pres AS FLOAT), qc FROM clean;
  INSERT INTO agg10m_out SELECT code, window_start, window_end, CAST(AVG(nh3) AS FLOAT), CAST(MAX(nh3) AS FLOAT), CAST(AVG(temp) AS FLOAT), COUNT(*)
                         FROM TABLE(TUMBLE(TABLE clean, DESCRIPTOR(ts), INTERVAL '10' MINUTES)) GROUP BY code, window_start, window_end;
  INSERT INTO emission_out SELECT code, ts_ms, cluster, nh3, temp, vent, LEAST(nh3, 50.0) * 17.031 / 24.45 * 0.001 * vent FROM emission;
  INSERT INTO emission_db SELECT code, CAST(ts AS TIMESTAMP(3)), LEAST(nh3, 50.0) * 17.031 / 24.45 * 0.001 * vent, CAST(nh3 AS FLOAT), CAST(temp AS FLOAT), vent FROM emission;
  INSERT INTO alert_out SELECT * FROM alerts;
  INSERT INTO alert_db SELECT code, CAST(TO_TIMESTAMP_LTZ(ts_ms, 3) AS TIMESTAMP(3)), kind, level, CAST(`value` AS FLOAT), detail FROM alerts;
END;
