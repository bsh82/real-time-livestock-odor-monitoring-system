# Airflow 3 (경량 이미지) + 레이크하우스 작업에 필요한 클라이언트 (Trino, PostgreSQL)
FROM apache/airflow:slim-3.3.2-python3.11
RUN pip install --no-cache-dir "trino==0.340.0" "psycopg[binary]==3.3.6" "psycopg2-binary==2.9.10"
