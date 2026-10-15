-- Airflow 메타데이터 DB (DAG 실행·작업 상태). 새 볼륨에서만 실행된다 — 기존 볼륨은 scripts/start_all.ps1 이 만든다.
CREATE DATABASE airflow;
