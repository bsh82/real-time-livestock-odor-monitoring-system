"""확산 엔진 서비스 (GPU).
입력: odor.emission(배출원별 배출량 Q), weather.wind(배출원 위치 바람), sim.clock
처리: 배출원별 단위 농도장(풍향 1° 캐시) × Q × (5 / U) 를 군집 격자(10 m)에 더해 합성 → 2 m 농도 [ppm]
출력: runtime/fields/<군집>_<버전>.png(색상 지도), _wind.json(100 m 바람 격자), odor.field(요약), odor.alert(주택·학교 영향), dispersion 표
HTTP(8010): /attribute(민원 기여도) /point(지점 예측) /whatif(가상 실험) /status
실행: python -m services.engine.main
"""
import glob, json, math, os, sys, threading, time
import numpy as np, pandas as pd, geopandas as gpd, psycopg
from PIL import Image
from scipy import ndimage
from pyproj import Transformer
from fastapi import FastAPI, HTTPException, Response
import orjson
import uvicorn
from confluent_kafka import Consumer, Producer
from services.common import config as C
from services.engine.model import Surrogate
from services.engine import viz, farm3d

FIELDS = os.path.join(C.RUNTIME, 'fields'); os.makedirs(FIELDS, exist_ok=True)
TO_WGS = Transformer.from_crs('EPSG:5186', 'EPSG:4326', always_xy=True)
TO_TM = Transformer.from_crs('EPSG:4326', 'EPSG:5186', always_xy=True)
G2PPM = 1000 * 24.45 / 17.031          # g/m³ → ppm (25 °C)
RES = 10.0; DOM = 2000.0
TAPER = viz.radial_window()  # 표시용: 배출원에서 650~950 m 사이를 부드럽게 0 으로 (낮은 농도까지 그릴 때 2 km 사각 경계가 안 보이게)


def receptor_kind(u):
    if not isinstance(u, str): return None
    if '교육연구' in u: return 'school'
    if '노유자' in u: return 'care'
    if '주택' in u: return 'home'
    return None


class Cluster:
    def __init__(self, cid, members, sur, measures):
        self.id, self.members = cid, members
        st = {c: sur.prepare(c) for c in members}
        self.left = min(s['left'] for s in st.values()); self.bottom = min(s['bottom'] for s in st.values())
        self.right = max(s['left'] for s in st.values()) + DOM; self.top = max(s['bottom'] for s in st.values()) + DOM
        self.nx = int(round((self.right - self.left) / RES)); self.ny = int(round((self.top - self.bottom) / RES))
        self.off = {c: (int(round((s['bottom'] - self.bottom) / RES)), int(round((s['left'] - self.left) / RES))) for c, s in st.items()}
        self.bounds = [list(TO_WGS.transform(x, y)) for x, y in
                       ((self.left, self.bottom), (self.left, self.top), (self.right, self.top), (self.right, self.bottom))]
        rec = []
        for c in members:
            try:
                g = gpd.read_file(f'{C.DATA}/sites/{c}/buildings.geojson')
            except Exception:
                continue
            g['kind'] = g.get('use_name', pd.Series([None] * len(g))).map(receptor_kind)
            g = g[g.kind.notna()]
            if len(g): rec.append(pd.DataFrame(dict(kind=g.kind, cx=g.geometry.centroid.x, cy=g.geometry.centroid.y)))
        R = pd.concat(rec).round({'cx': 0, 'cy': 0}).drop_duplicates(['cx', 'cy']) if rec else pd.DataFrame(columns=['kind', 'cx', 'cy'])
        R['iy'] = ((R.cy - self.bottom) // RES).astype(int); R['ix'] = ((R.cx - self.left) // RES).astype(int)
        self.rec = R[(R.iy >= 0) & (R.iy < self.ny) & (R.ix >= 0) & (R.ix < self.nx)].reset_index(drop=True)
        self.measures = [(code, int((y - self.bottom) // RES), int((x - self.left) // RES)) for code, x, y in measures
                         if 0 <= (y - self.bottom) // RES < self.ny and 0 <= (x - self.left) // RES < self.nx]
        self.version, self.sig, self.last, self.last_t, self.slot = 0, None, None, 0.0, None

    def cell(self, lon, lat):
        x, y = TO_TM.transform(lon, lat); iy, ix = int((y - self.bottom) // RES), int((x - self.left) // RES)
        return (iy, ix) if 0 <= iy < self.ny and 0 <= ix < self.nx else None

    def composite(self, fields, inputs):
        """inputs: {code: (θ, U, Q)} → 농도 [ppm] (ny, nx), 표시용 농도(가장자리 감쇠), 바람 u, v (100 m 격자), 배출원별 배율"""
        Ct = np.zeros((self.ny, self.nx), np.float32); Cd = np.zeros((self.ny, self.nx), np.float32)
        wy, wx = (self.ny + 9) // 10, (self.nx + 9) // 10
        Wu, Wv, Wn = np.zeros((wy, wx), np.float32), np.zeros((wy, wx), np.float32), np.zeros((wy, wx), np.float32)
        scales = {}
        for code, (th, U, Q) in inputs.items():
            c2m, u10, v10 = fields[(code, int(round(th)) % 360)]
            s = Q * 5.0 / max(U, C.U_MIN) * G2PPM; scales[code] = s
            iy, ix = self.off[code]; Ct[iy:iy + 200, ix:ix + 200] += c2m * s; Cd[iy:iy + 200, ix:ix + 200] += c2m * (TAPER * s)
            by, bx = iy // 10, ix // 10; hy, hx = min(20, wy - by), min(20, wx - bx)
            k = max(U, C.U_MIN) / 5.0
            Wu[by:by + hy, bx:bx + hx] += u10[:hy, :hx].astype(np.float32) * k
            Wv[by:by + hy, bx:bx + hx] += v10[:hy, :hx].astype(np.float32) * k; Wn[by:by + hy, bx:bx + hx] += 1
        m = Wn > 0
        if inputs:
            th = np.radians([v[0] for v in inputs.values()]); U = np.array([max(v[1], C.U_MIN) for v in inputs.values()])
            bu, bv = float(np.mean(-U * np.sin(th))), float(np.mean(-U * np.cos(th)))
        else:
            bu = bv = 0.0
        Wu = np.where(m, Wu / np.maximum(Wn, 1), bu); Wv = np.where(m, Wv / np.maximum(Wn, 1), bv)
        return Ct, Cd, Wu, Wv, scales

    def stats(self, Ct):
        r = self.rec; val = Ct[r.iy.values, r.ix.values] if len(r) else np.zeros(0)
        k = r.kind.values if len(r) else np.array([])
        iy, ix = np.unravel_index(int(np.argmax(Ct)), Ct.shape)
        lon, lat = TO_WGS.transform(self.left + (ix + 0.5) * RES, self.bottom + (iy + 0.5) * RES)
        a = RES * RES / 1e6
        return dict(cmax=round(float(Ct.max()), 3), cmax_lonlat=[lon, lat], area_06=round(float((Ct >= 0.6).sum() * a), 4),
                    area_10=round(float((Ct >= 1.0).sum() * a), 4),
                    homes_06=int(((k == 'home') & (val >= 0.6)).sum()), homes_10=int(((k == 'home') & (val >= 1.0)).sum()),
                    schools_06=int(((k == 'school') & (val >= 0.6)).sum()), care_06=int(((k == 'care') & (val >= 0.6)).sum()),
                    homes_total=int((k == 'home').sum()), home_max=round(float(val[k == 'home'].max()), 3) if (k == 'home').any() else 0.0,
                    measures=[dict(code=c, pred=round(float(Ct[iy_, ix_]), 3)) for c, iy_, ix_ in self.measures])

    def render(self, Ct, Wu, Wv, name, wind_sig=None):
        """농도 PNG 는 매번, 바람 격자 JSON 은 바람(풍향·풍속)이 바뀔 때만 새로 쓴다 → 화면이 같은 바람 파일을 다시 받지 않게"""
        rgba = viz.colorize(ndimage.gaussian_filter(Ct, 0.8))[::-1]
        Image.fromarray(np.ascontiguousarray(rgba), 'RGBA').save(os.path.join(FIELDS, f'{name}.png'), compress_level=1)
        if wind_sig is not None and wind_sig == getattr(self, 'wind_sig', None): return self.wind_name
        wname = f'{name}_wind'
        json.dump(dict(nx=int(Wu.shape[1]), ny=int(Wu.shape[0]), bounds=self.bounds,
                       u=np.round(Wu[::-1], 2).ravel().tolist(), v=np.round(Wv[::-1], 2).ravel().tolist()),
                  open(os.path.join(FIELDS, f'{wname}.json'), 'w'))
        if wind_sig is not None:
            old = getattr(self, 'wind_name', None)
            if old and os.path.exists(os.path.join(FIELDS, f'{old}.json')): os.remove(os.path.join(FIELDS, f'{old}.json'))
            self.wind_sig, self.wind_name = wind_sig, wname
        return wname


class Engine:
    def __init__(self):
        S = pd.read_csv(os.path.join(C.DATA, 'sources.csv'), encoding='utf-8-sig', dtype={'code': str})
        self.S = S.set_index('code')
        wp = pd.read_csv(os.path.join(C.DATA, 'weather_points.csv'), dtype={'code': str}) if os.path.exists(os.path.join(C.DATA, 'weather_points.csv')) else None
        self.key = dict(zip(wp.code, wp.key)) if wp is not None else {}
        for f in glob.glob(os.path.join(FIELDS, '*')): os.remove(f)          # 지난 실행의 확산장·바람·가상실험 파일 정리
        t0 = time.time(); self.sur = Surrogate(); print(f'models loaded on {self.sur.dev} in {time.time() - t0:.1f}s', flush=True)
        E = S[S.role != 'measure']; Mz = S[S.role == 'measure']
        self.clusters = {}
        for cid, g in E.groupby('cluster'):
            meas = [(r.code, r.x, r.y) for r in Mz[Mz.cluster == cid].itertuples()]
            self.clusters[cid] = Cluster(cid, list(g.code), self.sur, meas)
        print('clusters', len(self.clusters), 'emitters', len(E), 'receptors', sum(len(c.rec) for c in self.clusters.values()), flush=True)
        self.Q, self.wind, self.clock_ms = {}, {}, 0
        self.prod = Producer({'bootstrap.servers': C.KAFKA, 'linger.ms': 20})
        self.timing = dict(infer_ms=0.0, render_ms=0.0, renders=0)
        self.pg = None
        self.sigma = C.SIGMA_THETA          # 풍향 흔들림 σ_θ [°] (화면에서 바꿀 수 있음)

    # ---------- 입력 ----------
    def consume(self):
        cons = Consumer({'bootstrap.servers': C.KAFKA, 'group.id': 'engine', 'auto.offset.reset': 'latest'})
        cons.subscribe([C.TOPIC['emission'], C.TOPIC['wind'], C.TOPIC['clock']])
        while True:
            m = cons.poll(0.5)
            if m is None or m.error(): continue
            d = json.loads(m.value()); t = m.topic()
            if t == C.TOPIC['emission']: self.Q[d['code']] = (0.0 if d['code'] in C.SUSPECT else float(d['q_gs'] or 0.0), d['ts_ms'])   # 데이터 이상 센서는 확산 제외
            elif t == C.TOPIC['wind']: self.wind[d['key']] = (float(d['wd']), float(d['ws']), d['ts_ms'])
            else: self.clock_ms = d['replay_ms']

    def inputs(self, cl, wd=None, ws=None):
        out = {}
        for code in cl.members:
            w = self.wind.get(self.key.get(code))
            if w is None and wd is None: continue
            th = wd if wd is not None else w[0]; U = ws if ws is not None else w[1]
            out[code] = (th, U, self.Q.get(code, (0.0, 0))[0])
        return out

    # ---------- 합성·출력 ----------
    def update(self, cl, force=False):
        inp = self.inputs(cl)
        if not inp: return
        sigma = self.sigma                                                  # 이번 계산에 쓸 σ_θ (계산·표시를 같은 값으로)
        sig = {c: (int(round(th)) % 360, round(U, 1), q) for c, (th, U, q) in inp.items()}
        if not force and cl.sig is not None and getattr(cl, 'sig_sigma', None) == sigma and time.time() - cl.last_t < 15:
            same = all(c in cl.sig and cl.sig[c][0] == v[0] and abs(cl.sig[c][1] - v[1]) < 0.15 and
                       abs(cl.sig[c][2] - v[2]) <= 0.03 * max(cl.sig[c][2], 1e-4) for c, v in sig.items())
            if same: return
        t0 = time.time(); fields = self.sur.smooth_fields([(c, v[0]) for c, v in inp.items()], sigma); t1 = time.time()
        Ct, Cd, Wu, Wv, scales = cl.composite(fields, inp); st = cl.stats(Ct)
        cl.version += 1; name = f'{cl.id}_{cl.version}'
        wsig = tuple((c, v[0], round(v[1] * 2) / 2) for c, v in sorted(sig.items()))
        wname = cl.render(Cd, Wu, Wv, name, wind_sig=wsig); t2 = time.time()
        old = os.path.join(FIELDS, f'{cl.id}_{cl.version - 4}.png')
        if os.path.exists(old): os.remove(old)
        cl.sig, cl.sig_sigma, cl.last_t, cl.last = sig, sigma, time.time(), dict(inp=inp, scales=scales, Ct=Ct, fields=fields)
        self.timing['infer_ms'] = 0.8 * self.timing['infer_ms'] + 0.2 * (t1 - t0) * 1000
        self.timing['render_ms'] = 0.8 * self.timing['render_ms'] + 0.2 * (t2 - t1) * 1000; self.timing['renders'] += 1
        q_total = float(sum(v[2] for v in inp.values()))
        th = np.radians([v[0] for v in inp.values()]); U = np.array([v[1] for v in inp.values()])
        wd = float((np.degrees(np.arctan2(np.mean(U * np.sin(th)), np.mean(U * np.cos(th)))) + 360) % 360); ws = float(np.mean(U))
        msg = dict(cluster=cl.id, version=cl.version, ts_ms=self.clock_ms, png=f'/fields/{name}.png', wind=f'/fields/{wname}.json',
                   bounds=cl.bounds, wd=round(wd, 1), ws=round(ws, 2), q_total=round(q_total, 4), infer_ms=round((t1 - t0) * 1000, 1), sigma=sigma,
                   members=[dict(code=c, q=round(v[2], 5), wd=round(v[0], 1), ws=round(v[1], 2)) for c, v in inp.items()], **st)
        self.prod.produce(C.TOPIC['field'], json.dumps(msg), key=cl.id)
        if st['homes_10'] or st['homes_06'] or st['schools_06']:
            lvl = 'warning' if st['homes_10'] or st['schools_06'] else 'caution'
            det = f"주택 {st['homes_06']}동 0.6 ppm 이상 예측" + (f" (1 ppm 이상 {st['homes_10']}동)" if st['homes_10'] else '') + \
                  (f", 학교 {st['schools_06']}곳" if st['schools_06'] else '')
            self.prod.produce(C.TOPIC['alert'], json.dumps(dict(code=cl.id, ts_ms=self.clock_ms, kind='IMPACT', level=lvl, value=st['cmax'],
                                                                thr=0.6, cluster=cl.id, detail=det), ensure_ascii=False), key=cl.id)
        self.prod.poll(0)
        slot = self.clock_ms // 600000
        if slot != cl.slot:
            cl.slot = slot
            try:
                if self.pg is None or self.pg.closed: self.pg = psycopg.connect(C.PG_DSN, autocommit=True)
                self.pg.execute('INSERT INTO dispersion VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                                (cl.id, slot * 600, wd, ws, q_total, st['cmax'], st['area_06'], st['area_10'], st['homes_06'], st['homes_10'], st['schools_06']))
            except Exception as e:
                print('db error', e, flush=True); self.pg = None

    def loop(self):
        while True:
            t = time.time()
            for cl in self.clusters.values():
                try: self.update(cl)
                except Exception as e: print('update error', cl.id, repr(e), flush=True)
            time.sleep(max(0.2, 1.0 - (time.time() - t)))

    # ---------- 질의 ----------
    def locate(self, lon, lat):
        for cl in self.clusters.values():
            c = cl.cell(lon, lat)
            if c: return cl, c
        return None, None

    def attribute(self, lon, lat):
        cl, cell = self.locate(lon, lat)
        if cl is None or cl.last is None: return dict(cluster=None, total=0.0, sources=[])
        iy, ix = cell; out = []
        for code, (th, U, Q) in cl.last['inp'].items():
            oy, ox = cl.off[code]; py, px = iy - oy, ix - ox
            if not (0 <= py < 200 and 0 <= px < 200): continue
            c2m = cl.last['fields'].get((code, int(round(th)) % 360))
            if c2m is None: continue
            v = float(c2m[0][py, px] * cl.last['scales'][code])
            if v > 1e-4: out.append(dict(code=code, farm=self.S.at[code, 'farm'], loc=self.S.at[code, 'loc'], ppm=round(v, 4), q=round(Q, 5), wd=th, ws=U))
        tot = sum(o['ppm'] for o in out)
        for o in out: o['share'] = round(o['ppm'] / tot, 3) if tot > 0 else 0
        out.sort(key=lambda o: -o['ppm'])
        return dict(cluster=cl.id, total=round(tot, 4), sources=out)

    def whatif(self, cid, wd, ws, qscale=1.0):
        cl = self.clusters.get(cid)
        if cl is None: raise HTTPException(404, 'unknown cluster')
        inp = {c: (wd, ws, q * qscale) for c, (_, _, q) in self.inputs(cl, wd, ws).items()}
        sigma = self.sigma
        t0 = time.time(); fields = self.sur.smooth_fields([(c, wd) for c in inp], sigma); t1 = time.time()
        Ct, Cd, Wu, Wv, _ = cl.composite(fields, inp); st = cl.stats(Ct)
        name = f'whatif_{cid}_{int(time.time() * 1000)}'; cl.render(Cd, Wu, Wv, name)       # 13자리 ms → 문자열 정렬 = 시간 순
        for old in sorted(glob.glob(os.path.join(FIELDS, f'whatif_{cid}_*.png')))[:-6]:
            for f in (old, old[:-4] + '_wind.json'):
                if os.path.exists(f): os.remove(f)
        return dict(cluster=cid, png=f'/fields/{name}.png', wind=f'/fields/{name}_wind.json', bounds=cl.bounds,
                    infer_ms=round((t1 - t0) * 1000, 1), wd=wd, ws=ws, sigma=sigma, n_dirs=len(Surrogate.meander_plan(wd, sigma)[0]), sources=len(inp), **st)


ENGINE: Engine = None
app = FastAPI(title='OdorTwin dispersion engine')


@app.get('/status')
def status():
    return dict(device=ENGINE.sur.dev, cache=len(ENGINE.sur.cache), sigma=ENGINE.sigma, clusters=len(ENGINE.clusters), q=len(ENGINE.Q), wind=len(ENGINE.wind),
                clock_ms=ENGINE.clock_ms, **{k: round(v, 1) for k, v in ENGINE.timing.items()})


@app.get('/attribute')
def attribute(lat: float, lon: float): return ENGINE.attribute(lon, lat)


@app.get('/point')
def point(lat: float, lon: float):
    r = ENGINE.attribute(lon, lat); return dict(cluster=r['cluster'], ppm=r['total'])


@app.get('/whatif')
def whatif(cluster: str, wd: float, ws: float, qscale: float = 1.0): return ENGINE.whatif(cluster, wd % 360, max(ws, 0.1), qscale)


@app.get('/config')
def config(sigma: float = None):
    """풍향 흔들림 σ_θ 조회·변경 (0 = 끔). 바꾸면 모든 권역을 다시 계산한다."""
    if sigma is not None:
        ENGINE.sigma = float(min(max(sigma, 0.0), 10.0))
        for cl in ENGINE.clusters.values(): cl.sig = None
    return dict(sigma=ENGINE.sigma)


@app.get('/farm3d')
def farm3d_view(code: str):
    if code not in ENGINE.S.index: raise HTTPException(404, 'unknown code')
    try: return Response(orjson.dumps(farm3d.build(ENGINE, code), option=orjson.OPT_SERIALIZE_NUMPY), media_type='application/json')
    except (ValueError, RuntimeError) as e: raise HTTPException(409, str(e))


@app.get('/clusters')
def clusters():
    return [dict(id=c.id, members=c.members, bounds=c.bounds, nx=c.nx, ny=c.ny, receptors=int(len(c.rec)),
                 homes=int((c.rec.kind == 'home').sum()), schools=int((c.rec.kind == 'school').sum()), care=int((c.rec.kind == 'care').sum()))
            for c in ENGINE.clusters.values()]


def main():
    global ENGINE
    ENGINE = Engine()
    threading.Thread(target=ENGINE.consume, daemon=True).start()
    threading.Thread(target=ENGINE.loop, daemon=True).start()
    C.serve(app, 8010)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8'); main()
