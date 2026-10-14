"""데이터셋: (타깃) 14농가 CFD 단위농도장, (소스) 가우시안 플룸(lazy).
입력 23채널: u,v,w × 6층(18) + 지형 z_g + 건물 b + 배출원 s + 좌표 x,y.  출력 6채널: log1p(T/c0), 건물 마스크.
정규화: 바람 /U_REF, 지형 (z-min)/(max-min), 건물 /20 m, 좌표 [0,1]. c0=1e-6 (단위농도 플룸 규모 1e-5~1e-3 → 2~7).
분할: 학습 14농가 풍향 {0,120,240}=검증, 나머지=학습(바람장 발산 1건 제외). n_T 샘플링·풍향 홀드아웃 지원.
"""
import json, os, numpy as np, torch, xarray as xr
from torch.utils.data import Dataset
from . import gaussian as G

LEV = [0, 1, 2, 3, 4, 5]                 # z 인덱스 {2,5,10,20,30,50} m
C0 = 1e-6
VAL_DIRS = (0, 120, 240)
EXCLUDE = set()                          # 학습에서 뺄 (농가, 풍향) — 학습 설정은 비공개
CONC = os.environ.get('OPKCI_CONC', 'conc_unit')   # 농도 파일 sites/<fid>/<CONC>_<fid>.nc (2026-10-13: conc_nut = 난류 확산계수 ν_t/0.7 재계산본)
U_IDX = [0 + 3 * k for k in range(6)]   # 채널 배치: [u_k, v_k, w_k] × 6
V_IDX = [1 + 3 * k for k in range(6)]
N_IN, N_OUT = 23, 6


def encode_target(T):
    """T (K,H,W) 단위농도(NaN=건물) → (log1p(T/c0), mask)"""
    m = np.isfinite(T).astype(np.float32); t = np.where(np.isfinite(T), np.clip(T, 0, None), 0.0)
    return np.log1p(t / C0).astype(np.float32), m


def decode_target(y):
    return (np.expm1(y) * C0) if isinstance(y, np.ndarray) else torch.expm1(y) * C0


def static_channels(zg, bh, src_xy):
    """지형·건물·배출원·좌표 채널 (5,H,W)"""
    zg = np.nan_to_num(zg).astype(np.float32); zn = (zg - zg.min()) / (zg.max() - zg.min() + 1e-6)
    bn = np.nan_to_num(bh).astype(np.float32) / 20.0
    s = G.source_blob(src_xy)
    X, Y = np.meshgrid(G.XS / G.L, G.XS / G.L)
    return np.stack([zn, bn, s, X.astype(np.float32), Y.astype(np.float32)])


def build_input(uvw, static):
    """uvw (3,6,H,W) [m/s] → (18,H,W) 채널 순서 [u0,v0,w0,u1,…] /U_REF, + static(5) → (23,H,W)"""
    uvw = np.nan_to_num(uvw).astype(np.float32) / G.U_REF
    wind = np.transpose(uvw, (1, 0, 2, 3)).reshape(18, G.N, G.N)
    return np.concatenate([wind, static], 0)


class CFDData:
    """14농가 전부 메모리 적재. items: list of dict(fid, dir, x(23,H,W), y(6,H,W), m(6,H,W))"""
    def __init__(self, root, farms, test=False, keep_dirs=None):
        self.items = []
        for fid in farms:
            W = xr.open_dataset(f'{root}/sites/{fid}/wind_{fid}.nc'); Cc = xr.open_dataset(f'{root}/sites/{fid}/{CONC}_{fid}.nc')
            Gm = json.load(open(f'{root}/sites/{fid}/site.json', encoding='utf-8'))['geometry']
            src = (Gm['sensor_local']['x'], Gm['sensor_local']['y'])
            st = static_channels(W.z_ground.values, W.building_height.values, src)
            # CFD 가 아닌(이웃 보간) 풍향은 정답이 아니므로 뺀다
            interp = {int(r['dir']) for r in json.loads(Cc.attrs.get('runs', '[]')) if r.get('method', 'CFD') != 'CFD'}
            skipped = []
            for i, d in enumerate(W.dir.values):
                d = int(d)
                if (fid, d) in EXCLUDE: continue
                if keep_dirs is not None and d not in keep_dirs: continue
                if d not in Cc.dir.values: continue
                if d in interp: skipped.append(d); continue
                uvw = np.stack([W.u.values[i, LEV], W.v.values[i, LEV], W.w.values[i, LEV]])
                j = int(np.where(Cc.dir.values == d)[0][0])
                y, m = encode_target(Cc.T.values[j, LEV])
                self.items.append(dict(fid=fid, dir=d, x=build_input(uvw, st), y=y, m=m,
                                       zg=np.nan_to_num(W.z_ground.values).astype(np.float32), src=src))
            if skipped: print(f'[CFDData] {fid} {CONC}: 이웃 보간 풍향 제외 {skipped}', flush=True)
            W.close(); Cc.close()

    def split(self, val_dirs=VAL_DIRS, holdout_odd=False):
        """(train, val) 인덱스. holdout_odd: 짝수 풍향 학습/홀수 시험(E3)."""
        tr, va = [], []
        for k, it in enumerate(self.items):
            if holdout_odd:
                (va if (it['dir'] // 10) % 2 == 1 else tr).append(k)
            else:
                (va if it['dir'] in val_dirs else tr).append(k)
        return tr, va

    def subsample(self, idx, n_T, seed):
        """n_T 개 무작위 추출(농가·풍향 균등: 농가별 round-robin)."""
        if n_T is None or n_T >= len(idx): return list(idx)
        rng = np.random.default_rng(seed); by = {}
        for k in idx: by.setdefault(self.items[k]['fid'], []).append(k)
        for v in by.values(): rng.shuffle(v)
        out = []; farms = list(by); rng.shuffle(farms)
        while len(out) < n_T:
            for f in farms:
                if by[f] and len(out) < n_T: out.append(by[f].pop())
        return out


class CFDTorch(Dataset):
    def __init__(self, data, idx): self.d, self.idx = data, list(idx)
    def __len__(self): return len(self.idx)
    def __getitem__(self, i):
        it = self.d.items[self.idx[i]]
        return (torch.from_numpy(it['x']), torch.from_numpy(it['y']), torch.from_numpy(it['m']),
                torch.tensor(float(it['dir'])), torch.from_numpy(it['zg']), torch.tensor(it['src'], dtype=torch.float32))


class GaussianTorch(Dataset):
    """소스 도메인 lazy 생성: (θ, 안정도, H) 격자. 풍향 1° × 안정도 6 × H 3 = 6480."""
    def __init__(self, thetas=range(0, 360, 1), stabs='ABCDEF', Hs=(2., 3., 5.)):
        self.keys = [(t, s, h) for t in thetas for s in stabs for h in Hs]
        self.static = static_channels(np.zeros((G.N, G.N)), np.zeros((G.N, G.N)), (G.L / 2, G.L / 2))
        self.ones = np.ones((6, G.N, G.N), np.float32)
    def __len__(self): return len(self.keys)
    def __getitem__(self, i):
        t, s, h = self.keys[i]
        x = build_input(G.uniform_wind(t), self.static)
        y, _ = encode_target(G.plume(t, s, h))
        return (torch.from_numpy(x), torch.from_numpy(y), torch.from_numpy(self.ones), torch.tensor(float(t)),
                torch.zeros(G.N, G.N))


class GaussianSampler:
    """GPU on-the-fly 소스 샘플: θ~U[0,360), 안정도~U{A..F}, H~U{2,3,5}. 반환 x (B,23,N,N), y (B,6,N,N) 로그, m=1."""
    def __init__(self, device, Hs=(2., 3., 5.)):
        self.dev = device; self.Hs = torch.tensor(Hs, device=device)
        self.static = torch.from_numpy(static_channels(np.zeros((G.N, G.N)), np.zeros((G.N, G.N)), (G.L / 2, G.L / 2))).to(device)
    @torch.no_grad()
    def sample(self, B, gen=None):
        th = torch.rand(B, device=self.dev, generator=gen) * 360.0; st = torch.randint(0, 6, (B,), device=self.dev, generator=gen)
        H = self.Hs[torch.randint(0, len(self.Hs), (B,), device=self.dev, generator=gen)]
        y = torch.log1p(G.plume_torch(th, st, H) / C0)
        x = torch.cat([G.uniform_wind_torch(th) / G.U_REF, self.static[None].expand(B, -1, -1, -1)], 1)
        return x, y, torch.ones_like(y), th
