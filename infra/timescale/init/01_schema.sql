-- OdorTwin 운영 DB (TimescaleDB) + Iceberg JDBC 카탈로그 DB
CREATE DATABASE iceberg;
-- Flink(table.local-time-zone=Asia/Seoul)가 넘기는 시각을 KST 로 해석
ALTER DATABASE odor SET timezone TO 'Asia/Seoul';

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 센서·배출원 메타 (tools/prep_sensors.py → services/common/load_meta.py 로 적재)
CREATE TABLE sources (
  code text PRIMARY KEY, farm text, loc text, place text, pos text, role text, research text, livestock text,
  cluster text, lat double precision, lon double precision, x double precision, y double precision,
  vent_base double precision, nh3_p95 real
);

-- 실시간 원시값 (Flink odor.clean → JDBC)
CREATE TABLE sensor_raw (
  code text NOT NULL, ts timestamptz NOT NULL, nh3 real, h2s real, temp real, hum real, ws real, wd real,
  pm real, pres real, qc text, sent_at timestamptz, ingest_at timestamptz DEFAULT now()
);
SELECT create_hypertable('sensor_raw', by_range('ts', INTERVAL '1 day'));
CREATE INDEX ON sensor_raw (code, ts DESC);

-- 10분 집계 (Flink TUMBLE)
CREATE TABLE sensor_agg10m (
  code text NOT NULL, window_start timestamptz NOT NULL, window_end timestamptz, nh3_avg real, nh3_max real,
  temp_avg real, n bigint, PRIMARY KEY (code, window_start)
);
SELECT create_hypertable('sensor_agg10m', by_range('window_start', INTERVAL '7 days'));

-- 배출량 (Flink: 농도 × 환기량)
CREATE TABLE emission (
  code text NOT NULL, ts timestamptz NOT NULL, q_gs double precision, nh3 real, temp real, vent double precision,
  PRIMARY KEY (code, ts)
);
SELECT create_hypertable('emission', by_range('ts', INTERVAL '1 day'));

-- 경보 이벤트 (Flink 패턴 감지 결과, append)
CREATE TABLE alert_events (
  code text, ts timestamptz NOT NULL, kind text, level text, value real, detail text, created_at timestamptz DEFAULT now()
);
SELECT create_hypertable('alert_events', by_range('ts', INTERVAL '7 days'));

-- 경보 상태 (API 가 관리: active → ack → action → cleared)
CREATE TABLE alarms (
  id bigserial PRIMARY KEY, code text, cluster text, kind text, level text, state text DEFAULT 'active',
  first_ts timestamptz, last_ts timestamptz, value real, detail text,
  ack_by text, ack_at timestamptz, action text, action_at timestamptz, cleared_at timestamptz
);
CREATE TABLE alarm_log (id bigserial PRIMARY KEY, alarm_id bigint, at timestamptz DEFAULT now(), event text, actor text, note text);

-- 군집별 확산 요약 (확산 엔진)
CREATE TABLE dispersion (
  cluster text NOT NULL, ts timestamptz NOT NULL, wd real, ws real, q_total double precision, cmax_ppm real,
  area_06 real, area_10 real, homes_06 int, homes_10 int, schools_06 int, PRIMARY KEY (cluster, ts)
);
SELECT create_hypertable('dispersion', by_range('ts', INTERVAL '7 days'));

-- 기상 (기상 수집기)
CREATE TABLE weather (key text NOT NULL, ts timestamptz NOT NULL, wd real, ws real, src text, PRIMARY KEY (key, ts));

-- 민원
CREATE TABLE complaints (
  id bigserial PRIMARY KEY, ts timestamptz, lat double precision, lon double precision, place text, body text,
  intensity int, status text DEFAULT 'open', attribution jsonb, created_at timestamptz DEFAULT now()
);

-- 2023-02 ~ 2025-04 전체 이력 (레이크하우스 초기 적재용)
CREATE TABLE sensor_history (code text, ts timestamptz, nh3 real, h2s real, temp real, hum real, ws real, wd real, pm real, pres real);

-- 레이크하우스 배치 실행 기록
CREATE TABLE batch_runs (
  id bigserial PRIMARY KEY, job text, started_at timestamptz DEFAULT now(), finished_at timestamptz,
  rows bigint, status text, detail text
);

-- Iceberg JDBC 카탈로그 테이블 (Trino 는 직접 만들지 않으므로 미리 생성)
\connect iceberg
CREATE TABLE iceberg_namespace_properties (
  catalog_name VARCHAR(255) NOT NULL, namespace VARCHAR(255) NOT NULL, property_key VARCHAR(255), property_value VARCHAR(1000),
  PRIMARY KEY (catalog_name, namespace, property_key)
);
CREATE TABLE iceberg_tables (
  catalog_name VARCHAR(255) NOT NULL, table_namespace VARCHAR(255) NOT NULL, table_name VARCHAR(255) NOT NULL,
  metadata_location VARCHAR(1000), previous_metadata_location VARCHAR(1000),
  PRIMARY KEY (catalog_name, table_namespace, table_name)
);
