"""농장 1곳의 3D 확산 장면 데이터 (지도 축척에서는 안 보이는 지형·건물 효과를 보여 주기 위한 것).
- 같은 농장의 배출원(축사·액비저장조 등)을 클릭한 배출원의 2 km 격자에 합성한 6층(2·5·10·20·30·50 m) 농도 [ppm]
- 높이 2 m 간격으로 보간(로그 선형) → 20 m 격자 등농도면 (marching cubes)
- U-FNO 바람장(10·30 m)으로 지형을 따라가는 유선
- 지형(20 m), 음영기복 + 지상 2 m 농도 텍스처, 건물 외곽선·높이·용도·지상 농도
좌표: 영역 중심 원점, x = 동(m), y = 북(m), z = 영역 최저 지형 기준 높이(m). 지형 배열은 북쪽 행부터 (three.js PlaneGeometry 정점 순서).
"""
import base64, io, json, math, os, time
import numpy as np, geopandas as gpd
from PIL import Image
from scipy import ndimage
from skimage import measure
from services.common import config as C
from services.engine import viz
from services.engine.ml import wind as WD

ZL = np.array([2., 5., 10., 20., 30., 50.])
RES, N, HALF = 10.0, 200, 1000.0
G2PPM = 1000 * 24.45 / 17.031
NICE = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0]


def _bilinear(a, x, y):
    """a (행 = 남→북, 10 m 셀 중심 5, 15, ...) 에서 영역 좌표 x, y [m] 의 값"""
    xs, ys = np.atleast_1d(np.asarray(x, float)), np.atleast_1d(np.asarray(y, float))
    r = ndimage.map_coordinates(a, [(ys - 5) / RES, (xs - 5) / RES], order=1, mode='nearest')
    return r if np.ndim(x) else r[0]


def _place(dst, src, dy, dx):
    """src (..., 200, 200) 를 dst 의 (dy, dx) 칸 어긋난 위치에 더한다 (겹치는 부분만)"""
    y0, y1 = max(0, dy), min(N, N + dy); x0, x1 = max(0, dx), min(N, N + dx)
    if y1 <= y0 or x1 <= x0: return
    dst[..., y0:y1, x0:x1] += src[..., y0 - dy:y1 - dy, x0 - dx:x1 - dx]


RW = viz.radial_window()          # 표시용 원형 감쇠 (배출원 중심 650~950 m)
NH3_NOMINAL = 1.0                   # 센서 자료가 없거나 배출량 0 일 때 모양을 보여 주기 위한 가정 농도 [ppm]


def _levels(vmax):
    """보기 좋은 등농도면 값 3개: 실제로 면을 만드는 필드 최대값의 약 1 %, 6 %, 25 % 근처 '깔끔한' 값 (최대값의 절반 미만만)"""
    out = []
    for f in (0.01, 0.06, 0.25):
        t = vmax * f; v = min(NICE, key=lambda n: abs(math.log10(n) - math.log10(max(t, 1e-7))))
        if v < vmax * 0.5 and v not in out: out.append(v)
    return sorted(out)


def _nominal_q(S, c):
    role, place = S.at[c, 'role'], S.at[c, 'place']
    vent = C.VENT_BASE['proxy'] if role == 'proxy' else C.VENT_BASE['barn'] if place in C.BARN else C.VENT_BASE['storage']
    return NH3_NOMINAL * 17.031 / 24.45 * 1e-3 * vent * 0.7


def _streamlines(u, v, bh, zrel, agl, nseed=10, step=15.0, nstep=110):
    """한 높이(agl)의 수평 바람으로 유선 적분 (2차 룽게-쿠타, 씨앗점 전체를 한꺼번에). 건물(높이 > agl)이나 영역 밖, 무풍에서 멈춘다."""
    g = (np.arange(nseed) + 0.5) * (2 * HALF / nseed)
    x, y = [a.ravel().astype(np.float64) for a in np.meshgrid(g, g)]
    alive = np.ones(x.size, bool); hist = []
    for k in range(nstep):
        uu, vv = _bilinear(u, x, y), _bilinear(v, x, y); sp = np.hypot(uu, vv)
        alive &= sp >= 0.05
        sps = np.maximum(sp, 1e-6)
        xm, ym = x + 0.5 * step * uu / sps, y + 0.5 * step * vv / sps
        um, vm = _bilinear(u, xm, ym), _bilinear(v, xm, ym); spm = np.maximum(np.hypot(um, vm), 1e-6)
        if k % 2 == 0: hist.append((x.copy(), y.copy(), sp.copy(), alive.copy()))
        x = np.where(alive, x + step * um / spm, x); y = np.where(alive, y + step * vm / spm, y)
        inside = (x >= 0) & (x < 2 * HALF) & (y >= 0) & (y < 2 * HALF)
        alive &= inside
        iy, ix = np.clip((y // RES).astype(int), 0, N - 1), np.clip((x // RES).astype(int), 0, N - 1)
        alive &= bh[iy, ix] <= agl
        if not alive.any(): break
    lines = []
    for i in range(x.size):
        P = np.array([(hx[i], hy[i], hs[i]) for hx, hy, hs, ha in hist if ha[i]])
        if len(P) < 6: continue
        z = _bilinear(zrel, P[:, 0], P[:, 1]) + agl
        lines.append(dict(agl=agl, p=np.round(np.c_[P[:, 0] - HALF, P[:, 1] - HALF, z].ravel(), 1).tolist(), s=np.round(P[:, 2], 2).tolist()))
    return lines


def build(engine, code, wd=None, ws=None):
    """wd·ws 를 주면 농가의 모든 배출원에 그 바람을 쓴다 (지정 풍향 보기). 없으면 실시간 바람"""
    S = engine.S
    farm = S.at[code, 'farm']
    mem = [c for c in S.index[(S.farm == farm) & (S.role != 'measure')]]
    if not mem: raise ValueError('배출원이 없는 농가')
    base = code if code in mem else mem[0]
    sur = engine.sur; sb = sur.prepare(base)
    inp = {}
    for c in mem:
        w = engine.wind.get(engine.key.get(c))
        if wd is not None: w = (float(wd) % 360, float(ws) if ws is not None else (w[1] if w is not None else 3.0))
        if w is None: continue
        q = engine.Q.get(c, (0.0, 0))[0]
        inp[c] = (w[0], max(w[1], C.U_MIN), q if q > 0 else _nominal_q(S, c), q <= 0)
    if not inp: raise RuntimeError('바람 자료 대기 중')
    sigma = engine.sigma                                                     # 풍향 흔들림: 절대 2° 격자에서 θ ± 3σ_θ 가우시안 평균
    plans = {c: sur.meander_plan(v[0], sigma) for c, v in inp.items()}
    req, spans = [], {}
    for c, (angs, _) in plans.items():
        spans[c] = (len(req), len(angs)); req += [(c, float(a)) for a in angs] + [(c, inp[c][0])]   # 마지막 = 중심 풍향(유선용 바람)
    t0 = time.time(); res = sur.fields_3d(req); t1 = time.time()
    out = {}
    for c, (angs, wts) in plans.items():
        i0, n = spans[c]; acc = np.zeros_like(res[i0][0])
        for j, ww in enumerate(wts): acc += res[i0 + j][0] * ww
        out[c] = (acc, res[i0 + n][1])
    vol = np.zeros((6, N, N), np.float32); volw = np.zeros((6, N, N), np.float32); members = []
    for c, (th, U, Q, nominal) in inp.items():
        st = sur.static[c]; s = Q * 5.0 / U * G2PPM
        dy, dx = int(round((st['bottom'] - sb['bottom']) / RES)), int(round((st['left'] - sb['left']) / RES))
        C6 = out[c][0] * s
        _place(vol, C6, dy, dx); _place(volw, C6 * RW, dy, dx)          # vol: 수치용 원값, volw: 그림용(원형 감쇠)
        members.append(dict(code=c, place=S.at[c, 'place'], q=round(Q, 5), nominal=bool(nominal), wd=round(th, 1), ws=round(U, 2),
                            x=round(st['src'][0] + dx * RES - HALF, 1), y=round(st['src'][1] + dy * RES - HALF, 1)))
    thb, Ub = inp.get(base, next(iter(inp.values())))[:2]
    W = out[base if base in out else next(iter(out))][1] * (Ub / 5.0)        # 실제 풍속으로 환산
    zg, bh = sb['zg'], sb['bh']; zmin = float(zg.min()); zrel = zg - zmin
    for m in members: m['z'] = round(float(_bilinear(zrel, m['x'] + HALF, m['y'] + HALF)) + 2.0, 1)
    c2 = vol[0]; cmax = float(c2.max())

    # --- 등농도면: 높이 0~50 m 를 2 m 간격으로 보간(로그 선형), 수평 20 m 로 줄여 marching cubes ---
    zs = np.arange(0.0, 52.0, 2.0)
    j = np.clip(np.searchsorted(ZL, zs), 1, 5); wz = np.clip((zs - ZL[j - 1]) / (ZL[j] - ZL[j - 1]), 0, 1)
    lv = np.log(volw + 1e-7); V = np.exp(lv[j - 1] * (1 - wz)[:, None, None] + lv[j] * wz[:, None, None]) - 1e-7
    V2 = V.reshape(len(zs), N // 2, 2, N // 2, 2).mean((2, 4))
    Vp = np.pad(V2, ((0, 1), (1, 1), (1, 1)))                               # 윗면·옆면을 닫아 면이 열리지 않게
    gv = np.linspace(0.0, 2 * HALF, 101); TX, TY = np.meshgrid(gv, gv[::-1])            # PlaneGeometry 정점 위치 (북쪽 행부터)
    tz = _bilinear(zrel, np.clip(TX, 0, 1999.9).ravel(), np.clip(TY, 0, 1999.9).ravel()).reshape(101, 101)
    iso = []; vmax = float(V2.max()); used = set()
    for L in _levels(vmax):
        verts = faces = None
        for _ in range(3):                                                   # 면이 너무 작으면(정점 < 300) 한 단계 낮춘다
            if L in used or vmax <= L: break
            verts, faces, _, _ = measure.marching_cubes(Vp, level=L, spacing=(2.0, 20.0, 20.0))
            if len(verts) >= 300: break
            prev = max(used) if used else 0.0                                # 바깥 면보다 높은 값으로만 내려가야 면이 안쪽으로 겹친다
            lower = [n for n in NICE if prev < n < L]
            if not lower: break
            L = lower[-1]
        if verts is None or L in used: continue
        used.add(L)
        x = verts[:, 2] - 20 + 10; y = verts[:, 1] - 20 + 10; agl = verts[:, 0]
        z = _bilinear(zrel, np.clip(x, 0, 1999), np.clip(y, 0, 1999)) + agl
        iso.append(dict(ppm=L, v=np.round(np.c_[x - HALF, y - HALF, z].ravel(), 1).tolist(), f=faces.astype(int).ravel().tolist()))
    iso.sort(key=lambda s: s['ppm'])                                         # 바깥(낮은 농도) → 안쪽(높은 농도)

    # --- 바람 유선 (10 m, 30 m) ---
    lines = []
    for k, agl in ((2, 10.0), (4, 30.0)):
        lines += _streamlines(W[3 * k], W[3 * k + 1], bh, zrel, agl)

    # --- 지형 (20 m 격자, 북쪽 행부터) + 음영기복·농도 합성 텍스처 (5 m, 400×400) ---
    z5 = WD.read_tif(f'{C.DATA}/sites/{base}/terrain_5m.tif')
    hs = viz.hillshade(z5, 5.0)
    rel5 = (z5 - z5.min()) / max(z5.max() - z5.min(), 1.0)
    base_rgb = np.stack([22 + 50 * hs + 25 * rel5, 30 + 58 * hs + 20 * rel5, 40 + 62 * hs + 10 * rel5], -1)
    c5 = np.kron(volw[0], np.ones((2, 2), np.float32)); c5 = ndimage.uniform_filter(c5, 3)
    ov = viz.colorize(c5).astype(np.float32); a = ov[..., 3:4] / 255.0 * 0.95
    tex = (base_rgb * (1 - a) + ov[..., :3] * a).clip(0, 255).astype(np.uint8)[::-1]
    buf = io.BytesIO(); Image.fromarray(np.ascontiguousarray(tex), 'RGB').save(buf, 'PNG', compress_level=1)
    texture = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()        # 공유 파일 없이 응답에 포함 (동시 요청 안전)

    # --- 건물 ---
    blds = []
    try:
        g = gpd.read_file(f'{C.DATA}/sites/{base}/buildings.geojson')
    except Exception:
        g = None
    if g is not None and len(g):
        left, bottom = sb['left'], sb['bottom']
        for row in g.itertuples():
            geom = row.geometry
            if geom is None or geom.is_empty: continue
            polys = list(geom.geoms) if geom.geom_type == 'MultiPolygon' else [geom]
            u = getattr(row, 'use_name', None); u = u if isinstance(u, str) else ''
            kind = 'school' if '교육연구' in u else 'care' if '노유자' in u else 'home' if '주택' in u else 'barn' if ('동물' in u or '동.식물' in u) else 'other'
            h = float(getattr(row, 'height_m', 5.0) or 5.0)
            for p in polys:
                p = p.simplify(0.5)
                xy = np.asarray(p.exterior.coords)[:-1] - [left, bottom]
                if len(xy) < 3 or (xy < -50).all() or (xy > 2 * HALF + 50).all(): continue
                cx, cy = xy.mean(0)
                if not (0 <= cx < 2 * HALF and 0 <= cy < 2 * HALF): continue
                z0 = float(min(_bilinear(zrel, np.clip(xy[:, 0], 0, 1999), np.clip(xy[:, 1], 0, 1999))))
                blds.append(dict(p=np.round(xy - HALF, 1).ravel().tolist(), h=round(h, 1), z=round(z0, 1), k=kind,
                                 c=round(float(_bilinear(c2, cx, cy)), 4)))
    t2 = time.time()
    th = math.radians(thb)
    rs = S.at[base, 'research']; rs = rs if isinstance(rs, str) else ''
    return dict(code=base, clicked=code, farm=farm, loc=S.at[base, 'loc'], research=rs, wd=round(thb, 1), ws=round(Ub, 2),
                flow=[round(-math.sin(th), 3), round(-math.cos(th), 3)], members=members, cmax=round(cmax, 4),
                cmax_50m=round(float(vol[5].max()), 4), q_total=round(sum(m['q'] for m in members), 5),
                homes_06=sum(1 for b in blds if b['k'] == 'home' and b['c'] >= 0.6), homes=sum(1 for b in blds if b['k'] == 'home'),
                terrain=dict(n=101, size=2 * HALF, z=np.round(tz, 1).ravel().tolist(), zmax=round(float(zrel.max()), 1)),
                ground_z=round(float(_bilinear(zrel, HALF, HALF)), 1), nominal=any(m['nominal'] for m in members),
                suspect=next((C.SUSPECT[c] for c in inp if c in C.SUSPECT), None),     # 데이터 이상 센서 사유 (확산 계산 제외)
                texture=texture, iso=iso, streamlines=lines, buildings=blds,
                infer_ms=round((t1 - t0) * 1000, 1), build_ms=round((t2 - t1) * 1000, 1), ts_ms=engine.clock_ms,
                sigma=sigma, n_dirs=int(len(next(iter(plans.values()))[0])), override=wd is not None)
