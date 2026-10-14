"""124개소 센서 엑셀 → data/sensors.parquet (시계열) + data/sources.csv (센서 메타·역할·군집).
역할 규칙 (2026-10-13 합의): 시설 센서 = 배출원(source), 경계·입구·사무실·관리사 = 측정 지점(measure),
시설 센서가 없는 농가는 센서 하나를 근사 배출원(proxy)으로 둔다 (우선순위: 연구 농가 코드 > 농장부지 > 농장입구 > 사무실 > 관리사).
군집: 배출원 2 km 영역(센서 중심, EPSG:5186)이 겹치면 같은 군집 → 합성 농도장 단위.
실행 (demo 에서): .conda\\python.exe tools/prep_sensors.py <센서 엑셀 폴더>   (또는 ODOR_XLSX_DIR 환경 변수)
"""
import glob, os, re, sys, warnings
import numpy as np, pandas as pd
from pyproj import Transformer

warnings.filterwarnings('ignore')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.environ.get('ODOR_XLSX_DIR') or (sys.argv[1] if len(sys.argv) > 1 else '')   # 원본 센서 엑셀 폴더 (공개하지 않는 자료)
RESEARCH = os.environ.get('RESEARCH_FARMS', os.path.join(os.path.dirname(ROOT), 'farms_12.csv'))      # CFD 연구 농가 목록 (없으면 생략)
TEST = os.environ.get('TEST_FARMS', os.path.join(os.path.dirname(ROOT), 'test_farms.txt'))
if not SRC_DIR or not os.path.isdir(SRC_DIR): sys.exit('센서 엑셀 폴더를 인자나 ODOR_XLSX_DIR 로 지정하세요 (공개하지 않는 원본 자료)')
COLS = ['id', 'code', 'ts', 'nh3', 'h2s', 'co2', 'ch4', 'temp', 'hum', 'wd', 'ws', 'pm', 'pres']
FACILITY = {'비육돈사', '육성돈사', '분만돈사', '임신돈사', '산란계사', '육계사', '액비저장조', '분뇨처리장', '퇴비사'}
PROXY_ORDER = ['농장부지', '농장입구', '사무실', '관리사']
HALF = 1000.0
TF = Transformer.from_crs('EPSG:4326', 'EPSG:5186', always_xy=True)


def read_one(f):
    m = re.match(r'\d+_(.+)\((.+?)\)\((\d{8})\)_', os.path.basename(f))
    name, loc, code = m.groups()
    raw = pd.read_excel(f, header=None)
    lat, lon = [float(x) for x in str(raw.iloc[4, 1]).split(',')]
    d = raw.iloc[7:, :13].copy(); d.columns = COLS
    d['code'] = code; d['ts'] = pd.to_datetime(d['ts'], errors='coerce')
    for c in COLS[3:]: d[c] = pd.to_numeric(d[c], errors='coerce').astype('float32')
    d = d.dropna(subset=['ts']).drop(columns=['id']).sort_values('ts')
    place, pos = (loc.split(',') + [''])[:2]
    return d, dict(code=code, farm=name, loc=loc, place=place, pos=pos, lat=lat, lon=lon, n=len(d))


def main():
    files = sorted(f for f in glob.glob(os.path.join(SRC_DIR, '*.xlsx')) if not os.path.basename(f).startswith('~$'))
    parts, meta = [], []
    for i, f in enumerate(files):
        d, m = read_one(f); parts.append(d); meta.append(m)
        if i % 20 == 0: print(f'{i + 1}/{len(files)}', flush=True)
    S = pd.DataFrame(meta)
    rf = pd.read_csv(RESEARCH, encoding='utf-8-sig', dtype=str) if os.path.exists(RESEARCH) else pd.DataFrame(columns=['farm_id'])
    test = set(open(TEST).read().split()) if os.path.exists(TEST) else set()
    S['research'] = S.code.map(lambda c: ('test' if c in test else 'train') if c in set(rf.farm_id) else '')
    S['livestock'] = S.farm.map(S.groupby('farm').place.apply(lambda p: 'chicken' if p.isin(['산란계사', '육계사']).any() else 'pig'))
    S['role'] = np.where(S.place.isin(FACILITY), 'source', 'measure')
    for farm, g in S.groupby('farm'):
        if (g.role == 'source').any(): continue
        rank = g.apply(lambda r: (0 if r.research else 1, PROXY_ORDER.index(r.place) if r.place in PROXY_ORDER else 9), axis=1)
        S.loc[rank.sort_values().index[0], 'role'] = 'proxy'
    x, y = TF.transform(S.lon.values, S.lat.values); S['x'], S['y'] = x, y
    # 영역 원점: 센서 좌표를 5 m 로 반올림한 중심 − 1000 m (기존 sites/ 와 같은 규칙)
    S['x0'] = (np.round(S.x / 5) * 5 - HALF); S['y0'] = (np.round(S.y / 5) * 5 - HALF)
    em = S[S.role != 'measure']
    dx = np.abs(em.x.values[:, None] - em.x.values[None]); dy = np.abs(em.y.values[:, None] - em.y.values[None])
    adj = (dx < 2 * HALF) & (dy < 2 * HALF)
    lab = -np.ones(len(em), int); k = 0
    for i in range(len(em)):
        if lab[i] >= 0: continue
        stack = [i]; lab[i] = k
        while stack:
            j = stack.pop()
            for n in np.where(adj[j] & (lab < 0))[0]: lab[n] = k; stack.append(n)
        k += 1
    S.loc[em.index, 'cluster'] = lab
    for i in S.index[S.role == 'measure']:          # 측정 지점은 가장 가까운 배출원의 군집
        dd = np.hypot(em.x - S.at[i, 'x'], em.y - S.at[i, 'y']); S.at[i, 'cluster'] = S.at[dd.idxmin(), 'cluster']
    S['cluster'] = S.cluster.astype(int)
    order = S.groupby('cluster').size().sort_values(ascending=False).index          # 큰 군집부터 C01, C02 ...
    S['cluster'] = S.cluster.map({c: f'C{i + 1:02d}' for i, c in enumerate(order)})
    os.makedirs(os.path.join(ROOT, 'data'), exist_ok=True)
    S.to_csv(os.path.join(ROOT, 'data', 'sources.csv'), index=False, encoding='utf-8-sig')
    D = pd.concat(parts, ignore_index=True); D.to_parquet(os.path.join(ROOT, 'data', 'sensors.parquet'), index=False)
    print('rows', len(D), 'sensors', D.code.nunique())
    print('roles', S.role.value_counts().to_dict())
    C = S[S.role != 'measure'].groupby('cluster').agg(n=('code', 'size'), xmin=('x0', 'min'), xmax=('x0', 'max'), ymin=('y0', 'min'), ymax=('y0', 'max'))
    C['w_km'] = (C.xmax - C.xmin + 2 * HALF) / 1000; C['h_km'] = (C.ymax - C.ymin + 2 * HALF) / 1000
    print(C[['n', 'w_km', 'h_km']].sort_values('n', ascending=False).to_string())


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8'); main()
