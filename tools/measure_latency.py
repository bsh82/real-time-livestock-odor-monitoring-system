"""끝단 지연 측정 — 화면(브라우저) 자리에서 WebSocket /ws 를 받아 센서 송신 시각(sent_ms, 시뮬레이터 벽시계) 대비 지연을 잰다.
구간: 센서 송신 → MQTT → 브리지 → Kafka → Flink 정제 → Kafka → API → WebSocket 수신(이 스크립트 = 화면).
서버가 재는 KPI latency_ms(센서 송신 → API 수신, 중앙값) 도 같이 받아 비교한다. 같은 PC 라 시계 차는 없다.
실행: cd demo && .conda\\python.exe tools\\measure_latency.py --sec 60 [--out logs/latency.csv]
"""
import argparse, asyncio, json, time, os, statistics as st
import websockets

p = argparse.ArgumentParser(); p.add_argument('--url', default='ws://127.0.0.1:8000/ws'); p.add_argument('--sec', type=float, default=60); p.add_argument('--out', default='')
a = p.parse_args()


def pct(v, q): v = sorted(v); return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


async def main():
    e2e, srv_ingest, kpi_lat, rows = [], [], [], []
    t_end = time.time() + a.sec
    async with websockets.connect(a.url, max_size=None) as ws:
        print(f'connected {a.url}, measuring {a.sec:.0f}s …', flush=True)
        while time.time() < t_end:
            try: raw = await asyncio.wait_for(ws.recv(), timeout=t_end - time.time())
            except asyncio.TimeoutError: break
            now = time.time() * 1000; m = json.loads(raw)
            if m.get('type') == 'sensors':
                for it in m['items']:
                    s = it.get('sent_ms'); g = it.get('ingest_ms')
                    if not s: continue
                    e2e.append(now - s); rows.append((it['code'], s, g, now))
                    if g: srv_ingest.append(g - s)
            elif m.get('type') == 'kpi' and m['data'].get('latency_ms') is not None: kpi_lat.append(m['data']['latency_ms'])
    if not e2e: print('no samples (sensors not flowing?)'); return
    print(f'\nsamples {len(e2e)} msgs over {a.sec:.0f}s ({len(e2e) / a.sec:.1f} msg/s)')
    for name, v in (('센서 송신 → 화면 수신 (끝단)', e2e), ('센서 송신 → API 수신 (서버 ingest_ms)', srv_ingest)):
        if v: print(f'{name:36s} p50 {pct(v, .5):7.0f} ms   p95 {pct(v, .95):7.0f} ms   p99 {pct(v, .99):7.0f} ms   max {max(v):7.0f} ms   mean {st.mean(v):7.0f} ms')
    if kpi_lat: print(f'{"서버 KPI latency_ms (화면 표시값)":36s} p50 {pct(kpi_lat, .5):7.0f} ms   (KPI 샘플 {len(kpi_lat)}개, 중앙값의 중앙값)')
    if a.out:
        os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
        with open(a.out, 'w') as f:
            f.write('code,sent_ms,ingest_ms,recv_ms\n'); [f.write(f'{c},{s},{g},{int(r)}\n') for c, s, g, r in rows]
        print('saved', a.out)

asyncio.run(main())
