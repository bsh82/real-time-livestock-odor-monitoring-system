#!/bin/bash
# Flink 이미지에 넣을 커넥터·드라이버 jar 를 Maven Central 에서 받는다 → infra/flink/lib/
# 실행: bash tools/fetch_flink_jars.sh   (그다음 docker compose build flink-jobmanager flink-taskmanager)
set -eu
cd "$(dirname "$0")/../infra/flink/lib" 2>/dev/null || { mkdir -p "$(dirname "$0")/../infra/flink/lib"; cd "$(dirname "$0")/../infra/flink/lib"; }
M=https://repo1.maven.org/maven2
for u in \
  "$M/org/apache/flink/flink-sql-connector-kafka/3.4.0-1.20/flink-sql-connector-kafka-3.4.0-1.20.jar" \
  "$M/org/apache/flink/flink-connector-jdbc/3.3.0-1.20/flink-connector-jdbc-3.3.0-1.20.jar" \
  "$M/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar"; do
  f=$(basename "$u"); [ -s "$f" ] && { echo "있음  $f"; continue; }
  curl -fsSL -o "$f" "$u" && echo "받음  $f"
done
