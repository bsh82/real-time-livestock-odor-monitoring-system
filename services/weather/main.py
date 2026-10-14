"""기상 수집기.
재생 모드: Open-Meteo 과거 예보(JMA MSM 약 5 km, 키 불필요)에서 배출원 위치(소수 둘째 자리 반올림, 약 1 km)의 시간별 10 m 바람을 받아 캐시하고,
          재생 시계(sim.clock)에 맞춰 데이터 시각 10분마다 시간값 사이를 벡터 보간해 weather.wind 로 보낸다.
실행: python -m services.weather.main
"""
import json, math, os, sys, time
import httpx, numpy as np, pandas as pd
from confluent_kafka import Consumer, Producer
from services.common import config as C

CACHE = os.path.join(C.DATA, 'weather_msm.parquet'); POINTS = os.path.join(C.DATA, 'weather_points.csv')
SRC = 'JMA MSM (Open-Meteo)'; STEP = 600


def points():
    S = pd.read_csv(os.path.join(C.DATA, 'sources.csv'), encoding='utf-8-sig', dtype={'code': str})
    S['wlat'] = S.lat.round(2); S['wlon'] = S.lon.round(2); S['key'] = S.wlat.map('{:.2f}'.format) + '_' + S.wlon.map('{:.2f}'.format)
    S[['code', 'key', 'wlat', 'wlon']].to_csv(POINTS, index=False)
    return S.drop_duplicates('key')[['key', 'wlat', 'wlon']].reset_index(drop=True)


def fetch(P):
    if os.path.exists(CACHE): return pd.read_parquet(CACHE)
    d0 = (C.REPLAY_START - pd.Timedelta('1D')).date(); d1 = (C.REPLAY_END + pd.Timedelta('1D')).date(); rows = []
    for i in range(0, len(P), 20):
        p = P.iloc[i:i + 20]
        url = ('https://historical-forecast-api.open-meteo.com/v1/forecast?latitude=' + ','.join(p.wlat.astype(str)) +
               '&longitude=' + ','.join(p.wlon.astype(str)) + f'&start_date={d0}&end_date={d1}&hourly=wind_speed_10m,wind_direction_10m'
               '&models=jma_msm&wind_speed_unit=ms&timezone=Asia%2FSeoul')
        js = None
        for k in range(8):
            try: r = httpx.get(url, timeout=60); r.raise_for_status(); js = r.json(); break
            except Exception as e: print('retry', k, repr(e)[:120], flush=True); time.sleep(5)
        if js is None: raise RuntimeError('Open-Meteo 응답 실패')
        js = js if isinstance(js, list) else [js]
        for key, j in zip(p.key, js):
            h = j['hourly']
            rows.append(pd.DataFrame(dict(key=key, ts=pd.to_datetime(h['time']).tz_localize(C.KST), ws=h['wind_speed_10m'], wd=h['wind_direction_10m'])))
        print('fetched', i + len(p), '/', len(P), flush=True)
    W = pd.concat(rows, ignore_index=True); W.to_parquet(CACHE, index=False); return W


def main():
    P = points(); W = fetch(P).dropna()
    th = np.radians(W.wd.values); W['u'] = -W.ws * np.sin(th); W['v'] = -W.ws * np.cos(th)      # 바람이 향하는 방향 벡터
    G = {k: (C.epoch_s(g.ts), g.u.to_numpy(), g.v.to_numpy()) for k, g in W.groupby('key')}
    print('weather points', len(G), flush=True)
    prod = Producer({'bootstrap.servers': C.KAFKA, 'linger.ms': 20})
    cons = Consumer({'bootstrap.servers': C.KAFKA, 'group.id': 'weather', 'auto.offset.reset': 'latest'}); cons.subscribe([C.TOPIC['clock']])
    last = None
    while True:
        m = cons.poll(1.0)
        if m is None or m.error(): continue
        now = json.loads(m.value())['replay_ms'] / 1000; slot = int(now // STEP) * STEP
        if slot == last: continue
        last = slot
        for key, (t, u, v) in G.items():
            uu, vv = float(np.interp(slot, t, u)), float(np.interp(slot, t, v))
            ws = math.hypot(uu, vv); wd = (math.degrees(math.atan2(-uu, -vv)) + 360) % 360      # 기상학적 풍향(불어오는 방향)
            prod.produce(C.TOPIC['wind'], json.dumps(dict(key=key, ts_ms=slot * 1000, wd=round(wd, 1), ws=round(ws, 2), src=SRC)), key=key)
        prod.poll(0)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8'); main()
