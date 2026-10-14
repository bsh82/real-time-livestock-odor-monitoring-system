"""배출원 101곳의 2 km × 2 km 입력 래스터 생성 → data/sites/<code>/
연구 농가는 연구 폴더(../sites)의 기존 파일을 복사한다(CFD 학습·검증과 같은 영역).
나머지는 같은 규칙으로 만든다 (Copernicus DSM, NSDI 건물 + Overture 보충, ESA WorldCover z0).
출력: terrain_5m.tif, building_height_5m.tif, z0_5m.tif, landcover_5m.tif, buildings.geojson, site.json
입력 원자료 경로는 환경 변수 DEM_SRC, NSDI_SHP, WORLDCOVER 로 바꿀 수 있다 (공개 자료: Copernicus GLO-30 N33E126, NSDI AL_D010 제주, ESA WorldCover 2021 N33E126).
실행 (demo 에서): .conda\\python.exe tools\\build_sites.py
"""
import json, math, os, shutil, subprocess, sys, datetime
import numpy as np, pandas as pd, geopandas as gpd, pyogrio, rasterio
from rasterio.warp import reproject, Resampling
from rasterio.transform import from_origin
from rasterio import features
from shapely.geometry import box
from pyproj import Transformer
from scipy import ndimage

DEMO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); OPK = os.path.dirname(DEMO)
CRS = 'EPSG:5186'; RES = 5.0; HALF = 1000.0; N = int(2 * HALF / RES); FLOOR_H = 3.0
DEM_SRC = os.environ.get('DEM_SRC', f'{OPK}/data/dem_copernicus/Copernicus_DSM_COG_10_N33_00_E126_00_DEM.tif')
NSDI_SHP = os.environ.get('NSDI_SHP', f'{OPK}/data/buildings_nsdi/AL_D010_50_20260909/AL_D010_50_20260909.shp')
WC = os.environ.get('WORLDCOVER', f'{OPK}/data/worldcover/ESA_WorldCover_10m_2021_v200_N33E126_Map.tif')
OVERTURE_EXE = shutil.which('overturemaps') or os.path.join(os.path.dirname(sys.executable), 'Scripts', 'overturemaps.exe')
USE_H = {'동물및식물관련시설': 6.0, '창고시설': 7.0, '공장': 8.0, '단독주택': 6.0, '공동주택': 12.0, '제1종근린생활시설': 7.0,
         '제2종근린생활시설': 7.0, '교육연구시설': 10.0, '업무시설': 10.0, '숙박시설': 10.0}
DEFAULT_H = 4.0
OV_CLASS_H = {'farm_auxiliary': 6.0, 'barn': 6.0, 'greenhouse': 4.0, 'warehouse': 7.0, 'industrial': 8.0, 'house': 6.0, 'commercial': 7.0, 'shed': 3.0}
OV_DEFAULT_H = 5.0
Z0 = {10: 1.0, 20: 0.3, 30: 0.03, 40: 0.1, 50: 0.5, 60: 0.01, 70: 0.001, 80: 0.0002, 90: 0.05, 95: 0.5, 100: 0.02}
TF = Transformer.from_crs('EPSG:4326', CRS, always_xy=True)
COPY = ['terrain_5m.tif', 'building_height_5m.tif', 'z0_5m.tif', 'landcover_5m.tif', 'buildings.geojson', 'site.json']


def _num(v):
    try: v = float(v)
    except (TypeError, ValueError): return None
    return v if v == v and v > 0 else None


def bld_height(row):
    h = _num(row.get('height_raw')); nf = _num(row.get('floors_above'))
    if h is not None and 2.0 <= h <= 150.0: return h, 'nsdi_height'
    if nf is not None: return nf * FLOOR_H, 'floors*3'
    u = row.get('use_name'); u = u.replace(' ', '') if isinstance(u, str) else ''
    return (USE_H[u], f'use_default:{u}') if u in USE_H else (DEFAULT_H, 'default')


def overture(code, lat, lon):
    out = f'{DEMO}/data/overture/{code}.parquet'
    if os.path.exists(out) and os.path.getsize(out) > 0: return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    h = 1300.0; dlat = h / 111320; dlon = h / (111320 * math.cos(math.radians(lat)))
    bbox = f'{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}'
    try:
        subprocess.run([OVERTURE_EXE, 'download', f'--bbox={bbox}', '-f', 'geoparquet', '--type=building', '-o', out],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    except Exception as e:
        print('  overture fail', code, e); return None
    return out


def build(r, nsdi, dem, wc):
    code = r.code; out = f'{DEMO}/data/sites/{code}'; os.makedirs(out, exist_ok=True)
    cx, cy = TF.transform(r.lon, r.lat)
    x0 = math.floor((cx - HALF) / RES) * RES; y1 = math.ceil((cy + HALF) / RES) * RES; tr = from_origin(x0, y1, RES, RES)
    prof = dict(driver='GTiff', height=N, width=N, count=1, dtype='float32', crs=CRS, transform=tr, nodata=-9999.0, compress='deflate')
    terrain = np.full((N, N), np.nan, dtype='float32')
    reproject(rasterio.band(dem, 1), terrain, dst_transform=tr, dst_crs=CRS, resampling=Resampling.cubic, dst_nodata=np.nan, src_nodata=dem.nodata)
    if np.isnan(terrain).any():
        idx = ndimage.distance_transform_edt(np.isnan(terrain), return_distances=False, return_indices=True); terrain = terrain[tuple(idx)]
    terrain = np.maximum(terrain, 0.0)
    with rasterio.open(f'{out}/terrain_5m.tif', 'w', **prof) as d: d.write(terrain, 1)
    roi = box(x0, y1 - N * RES, x0 + N * RES, y1)
    g = nsdi.iloc[nsdi.sindex.query(roi, predicate='intersects')].copy()
    hs = [bld_height(row) for row in g.to_dict('records')]
    g['height_m'] = [h for h, _ in hs]; g['height_src'] = [s for _, s in hs]; g['src_dataset'] = 'NSDI'
    ovp = overture(code, r.lat, r.lon)
    if ovp:
        try:
            ov = gpd.read_parquet(ovp).to_crs(CRS); ov = ov[ov.intersects(roi)]
            un = g.geometry.union_all() if len(g) else None
            keep = [gm.area > 0 and (un is None or gm.intersection(un).area / gm.area < 0.3) for gm in ov.geometry]
            ov = ov[keep]
            if len(ov):
                cls = ov['class'] if 'class' in ov else pd.Series([None] * len(ov), index=ov.index)
                add = gpd.GeoDataFrame(dict(use_name=['동물및식물관련시설(추정)' if c == 'farm_auxiliary' else (c or '') for c in cls],
                                            height_m=[OV_CLASS_H.get(c, OV_DEFAULT_H) if isinstance(c, str) else OV_DEFAULT_H for c in cls],
                                            height_src='overture_class_default', src_dataset='Overture'), geometry=list(ov.geometry), crs=CRS)
                g = gpd.GeoDataFrame(pd.concat([g, add], ignore_index=True), geometry='geometry', crs=CRS)
        except Exception as e:
            print('  overture read fail', code, e)
    keep = [c for c in ['pnu', 'addr', 'use_name', 'bldg_name', 'height_raw', 'floors_above', 'height_m', 'height_src', 'src_dataset', 'geometry'] if c in g]
    g = g[keep]; g.to_file(f'{out}/buildings.geojson', driver='GeoJSON')
    bh = np.zeros((N, N), dtype='float32')
    if len(g):
        bh = features.rasterize(((gm, h) for gm, h in zip(g.geometry, g['height_m'])), out_shape=(N, N), transform=tr, fill=0.0, dtype='float32')
    with rasterio.open(f'{out}/building_height_5m.tif', 'w', **prof) as d: d.write(bh, 1)
    lc = np.zeros((N, N), dtype='uint8')
    reproject(rasterio.band(wc, 1), lc, dst_transform=tr, dst_crs=CRS, resampling=Resampling.nearest)
    z0 = np.full((N, N), 0.05, dtype='float32')
    for k, v in Z0.items(): z0[lc == k] = v
    p2 = dict(prof, nodata=None)
    with rasterio.open(f'{out}/z0_5m.tif', 'w', **p2) as d: d.write(z0, 1)
    with rasterio.open(f'{out}/landcover_5m.tif', 'w', **dict(p2, dtype='uint8')) as d: d.write(lc, 1)
    site = dict(farm_id=code, name=r.farm, sensor_loc=r.loc, center_wgs84=dict(lat=r.lat, lon=r.lon), center_epsg5186=dict(x=cx, y=cy),
                crs=CRS, res_m=RES, size_m=2 * HALF, shape=[N, N], bounds_epsg5186=dict(left=x0, bottom=y1 - N * RES, right=x0 + N * RES, top=y1),
                terrain=dict(source='Copernicus GLO-30 DSM', zmin=float(terrain.min()), zmax=float(terrain.max()), z_center=float(terrain[N // 2, N // 2])),
                buildings=dict(source='NSDI AL_D010 제주 + Overture 보충', count=int(len(g))),
                roughness=dict(source='ESA WorldCover 10 m v200', z0_area_mean=float(z0.mean())),
                geometry=dict(local_origin_epsg5186=dict(x=x0, y=y1 - N * RES), sensor_local=dict(x=cx - x0, y=cy - (y1 - N * RES), z_ground=float(terrain[N // 2, N // 2]))),
                generated_by='demo/tools/build_sites.py', created=datetime.datetime.now().isoformat(timespec='seconds'))
    json.dump(site, open(f'{out}/site.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    return len(g)


def main():
    S = pd.read_csv(f'{DEMO}/data/sources.csv', encoding='utf-8-sig', dtype={'code': str})
    E = S[S.role != 'measure'].reset_index(drop=True)
    todo = []
    for r in E.itertuples():
        out = f'{DEMO}/data/sites/{r.code}'
        if os.path.exists(f'{out}/site.json') and os.path.exists(f'{out}/z0_5m.tif'): continue
        src = f'{OPK}/sites/{r.code}'
        if os.path.exists(f'{src}/z0_5m.tif'):
            os.makedirs(out, exist_ok=True)
            for f in COPY: shutil.copy2(f'{src}/{f}', out)
            print('copied research site', r.code); continue
        todo.append(r)
    print(f'emitters {len(E)}, to build {len(todo)}', flush=True)
    if not todo: return
    xs, ys = TF.transform([r.lon for r in todo], [r.lat for r in todo])
    bb = (min(xs) - 1500, min(ys) - 1500, max(xs) + 1500, max(ys) + 1500)
    nsdi = pyogrio.read_dataframe(NSDI_SHP, bbox=bb, encoding='cp949')
    nsdi = nsdi.rename(columns={'A2': 'pnu', 'A4': 'addr', 'A9': 'use_name', 'A16': 'height_raw', 'A24': 'bldg_name', 'A26': 'floors_above'})
    nsdi = nsdi[[c for c in ['pnu', 'addr', 'use_name', 'height_raw', 'bldg_name', 'floors_above', 'geometry'] if c in nsdi]]
    print('NSDI buildings loaded', len(nsdi), flush=True)
    dem, wc = rasterio.open(DEM_SRC), rasterio.open(WC)
    for i, r in enumerate(todo):
        n = build(r, nsdi, dem, wc); print(f'[{i + 1}/{len(todo)}] {r.code} {r.farm} buildings={n}', flush=True)
    print('SITES_DONE')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8'); main()
