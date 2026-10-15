#!/bin/bash
# 오프라인 배경 지도 준비 → data/basemap/ (git 제외)
#   jeju.pmtiles : Protomaps 일일 빌드(OSM, ODbL)에서 제주 범위만 추출 (약 10 MB)
#   sprites/, fonts/ : Protomaps basemaps-assets (dark 스프라이트, 영문 글리프 — 한글은 브라우저 글꼴로 그림)
# 필요: pmtiles CLI (https://github.com/protomaps/go-pmtiles/releases) — PATH 또는 .tools/pmtiles.exe
# 실행: bash tools/fetch_basemap.sh [빌드 날짜 YYYYMMDD]
set -eu
cd "$(dirname "$0")/.."
PM=$(command -v pmtiles || echo .tools/pmtiles.exe)
D=${1:-$(date -u -d yesterday +%Y%m%d 2>/dev/null || date -u +%Y%m%d)}
OUT=data/basemap; A=https://protomaps.github.io/basemaps-assets
mkdir -p "$OUT/sprites" "$OUT/fonts"
"$PM" extract "https://build.protomaps.com/$D.pmtiles" "$OUT/jeju.pmtiles" --bbox=126.05,33.05,127.0,33.62 --maxzoom=15
for s in dark dark@2x; do for e in json png; do curl -fsSL -o "$OUT/sprites/$s.$e" "$A/sprites/v4/$s.$e"; done; done
for f in "Noto Sans Regular" "Noto Sans Medium" "Noto Sans Italic"; do
  mkdir -p "$OUT/fonts/$f"
  for r in 0-255 256-511 512-767 768-1023 8192-8447 8448-8703 9472-9727; do curl -fsSL -o "$OUT/fonts/$f/$r.pbf" "$A/fonts/${f// /%20}/$r.pbf"; done
done
ls -la "$OUT"
