"""바람장 예측 실험 공통 모듈 (2026-10-08). 지형·건물·거칠기·유입 풍향 → AGL 6층 u, v, w.
입력 19채널: 배경풍 u,v × 6층(12) + 상대고도 + 경사 ∂z/∂x, ∂z/∂y + 건물 높이 + log10 z0 + 좌표 x, y.
  배경풍 = CFD 유입 경계조건과 같은 중립 로그 프로파일(농가별 z0_inlet, U(10 m) = 5 m/s)을 도메인 전체에 편 것. CFD 없이 알 수 있는 값.
  정규화: 바람 /U_REF, 상대고도 (z − 평균)/50 m, 경사 무차원, 건물 /20 m, log10 z0 + 1 (z0 0.01~1 m → −1~1), 좌표 [0,1].
출력 18채널: [u_k, v_k, w_k] × 6층 /U_REF (농도 모델 입력과 같은 배치). 모델 출력 = 배경풍 + 네트워크 보정 (w 는 보정만).
마스크: CFD 의 건물 내부 NaN. 하류(농도) 입력에서는 건물 높이로 만든 마스크(bh > z_k)를 써서 CFD 없이도 같은 처리를 한다.
래스터(z0_5m.tif)는 북쪽이 위(행 0 = 북) → 상하 반전 후 2×2 로그평균으로 10 m 격자에 맞춘다 (terrain_5m.tif 로 정렬 확인: 상관 0.9997).
"""
import json, math, numpy as np, torch, torch.nn as nn, torch.nn.functional as F, xarray as xr
from PIL import Image
from . import gaussian as G, d4, models as M

LEV = [0, 1, 2, 3, 4, 5]                       # z 인덱스 {2,5,10,20,30,50} m
ZL = np.array([2., 5., 10., 20., 30., 50.])
N_IN, N_OUT = 19, 18
X_BG_U = [2 * k for k in range(6)]; X_BG_V = [2 * k + 1 for k in range(6)]          # 입력 배경풍 u,v
X_VEC = list(zip(X_BG_U, X_BG_V)) + [(13, 14)]                                         # 입력 벡터 쌍 (경사도 벡터)
Y_U = [3 * k for k in range(6)]; Y_V = [3 * k + 1 for k in range(6)]; Y_W = [3 * k + 2 for k in range(6)]
Y_VEC = list(zip(Y_U, Y_V))
VAL_DIRS = (0, 120, 240); EXCLUDE = set()      # 학습에서 뺄 (농가, 풍향) — 학습 설정은 비공개 (데모 추론에는 쓰지 않음)


def log_profile(z, z0, uref=G.U_REF, zref=G.Z_REF):
    ust = G.KAPPA * uref / np.log((zref + z0) / z0); return ust / G.KAPPA * np.log((z + z0) / z0)


def background(theta, z0_inlet):
    """(12,H,W) 배경풍 [u_k, v_k] × 6층 /U_REF — 유입 로그 프로파일을 유동 방향으로."""
    f = G.flow_dir(theta); U = log_profile(ZL, z0_inlet) / G.U_REF
    out = np.empty((12, G.N, G.N), np.float32)
    for k in range(6): out[2 * k] = U[k] * f[0]; out[2 * k + 1] = U[k] * f[1]
    return out


def read_tif(path):
    return np.array(Image.open(path), dtype=np.float32)[::-1].copy()           # 북쪽 위 → y 오름차순


def static_channels(root, fid, zg, bh):
    """(7,H,W): 상대고도, 경사 x, 경사 y, 건물, log10 z0, 좌표 x, y"""
    zg = np.nan_to_num(zg).astype(np.float32)
    gy, gx = np.gradient(zg, G.DX)                                                   # 행 = y, 열 = x
    z0 = read_tif(f'{root}/sites/{fid}/z0_5m.tif').clip(1e-3, None)
    lz = np.log10(z0).reshape(G.N, 2, G.N, 2).mean((1, 3))                          # 2×2 로그평균
    X, Y = np.meshgrid(G.XS / G.L, G.XS / G.L)
    return np.stack([(zg - zg.mean()) / 50.0, gx, gy, np.nan_to_num(bh) / 20.0, lz + 1.0, X, Y]).astype(np.float32)


def cont_factor(zrel, theta, lid_H=600.0):
    """막힘 연속 계수 F (H,W) = D(상류 유입 경계 점)/D(x), D = lid_H + (max z − z): CFD 윗면(최고 지형 + 600 m 미끄럼 경계)에 의한
    질량 보존 가속을 직선 유선으로 근사. zrel = 입력 채널 0 = (z − 평균)/50.
    CFD 없이 지형·풍향만으로 계산된다."""
    z = zrel * 50.0; D = lid_H + (z.max() - z)
    th = math.radians(theta); fx, fy = -math.sin(th), -math.cos(th)
    X, Y = np.meshgrid(G.XS, G.XS); big = np.full_like(X, 1e9)
    tx = X / fx if fx > 1e-6 else ((G.L - X) / (-fx) if fx < -1e-6 else big)
    ty = Y / fy if fy > 1e-6 else ((G.L - Y) / (-fy) if fy < -1e-6 else big)
    t = np.minimum(tx, ty)
    ix = np.clip((np.clip(X - t * fx, 0, G.L - 1e-3) / G.DX).astype(int), 0, G.N - 1)
    iy = np.clip((np.clip(Y - t * fy, 0, G.L - 1e-3) / G.DX).astype(int), 0, G.N - 1)
    return (D[iy, ix] / D).astype(np.float32)


def bld_mask(bh):
    """건물 높이로 만든 유체 마스크 (6,H,W): bh ≤ z_k 이면 1."""
    bh = np.nan_to_num(bh)
    return np.stack([(bh <= z).astype(np.float32) for z in ZL])


class WindData:
    """농가 목록의 바람장 적재. items: dict(fid, dir, y(18,H,W) /U_REF, m(6,H,W) uint8). x 는 get_x 로 즉석 생성.
    use_F: 입력 끝에 막힘 연속 계수 채널(F − 1) 추가 → 20채널 (2026-10-11 F 채널 실험)."""
    def __init__(self, root, farms, keep_dirs=None, drop_dirs=None, use_F=False):
        self.items, self.static, self.z0in, self.bmask, self.use_F = [], {}, {}, {}, use_F
        for fid in farms:
            W = xr.open_dataset(f'{root}/sites/{fid}/wind_{fid}.nc')
            zg, bh = W.z_ground.values, W.building_height.values
            self.static[fid] = static_channels(root, fid, zg, bh); self.z0in[fid] = float(W.attrs['z0_inlet_m']); self.bmask[fid] = bld_mask(bh)
            u, v, w = (W[c].values[:, LEV] for c in ('u', 'v', 'w'))
            for i, d in enumerate(W.dir.values):
                d = int(d)
                if (fid, d) in EXCLUDE or (keep_dirs is not None and d not in keep_dirs) or (drop_dirs is not None and d in drop_dirs): continue
                y = np.stack([u[i], v[i], w[i]], 1).reshape(18, G.N, G.N)                   # [u0,v0,w0,u1,...]
                m = np.isfinite(u[i]) & np.isfinite(v[i]) & np.isfinite(w[i])
                self.items.append(dict(fid=fid, dir=d, y=(np.nan_to_num(y) / G.U_REF).astype(np.float32), m=m.astype(np.uint8)))
            W.close()

    def get_x(self, it):
        x = [background(it['dir'], self.z0in[it['fid']]), self.static[it['fid']]]
        if self.use_F: x.append(cont_factor(self.static[it['fid']][0], it['dir'])[None] - 1.0)
        return np.concatenate(x, 0)

    def tensors(self, idx=None):
        idx = range(len(self.items)) if idx is None else idx
        its = [self.items[i] for i in idx]
        x = torch.from_numpy(np.stack([self.get_x(i) for i in its])); y = torch.from_numpy(np.stack([i['y'] for i in its]))
        m = torch.from_numpy(np.stack([i['m'] for i in its]).astype(np.float32))
        return x, y, m


def m18(m6):
    """(B,6,H,W) → (B,18,H,W) (u,v,w 같은 마스크)"""
    return m6.repeat_interleave(3, dim=1)


def base_from_x(x):
    """모델 잔차 기준: 배경풍 (B,18,H,W), w = 0"""
    b = torch.zeros(x.shape[0], 18, *x.shape[-2:], device=x.device, dtype=x.dtype)
    b[:, Y_U] = x[:, X_BG_U]; b[:, Y_V] = x[:, X_BG_V]; return b


def augment(x, y, m, g):
    """D4 변환 g: 입력·출력의 수평 벡터 쌍은 성분 회전, 나머지는 스칼라. 좌표 채널도 격자와 함께 이동."""
    _, R, op = d4.D4[g]; x2, y2 = x.clone(), y.clone()
    for t, pairs in ((x2, X_VEC), (y2, Y_VEC)):
        src = t.clone()
        for ui, vi in pairs:
            t[:, ui] = R[0, 0] * src[:, ui] + R[0, 1] * src[:, vi]; t[:, vi] = R[1, 0] * src[:, ui] + R[1, 1] * src[:, vi]
    return op(x2), op(y2), op(m)


# ---------- 모델 ----------
class UNet(nn.Module):
    """3단 U-Net (200→100→50→25), GroupNorm·GELU. 약 2 M 매개변수."""
    def __init__(self, n_in=N_IN, n_out=N_OUT, base=32):
        super().__init__()
        def blk(i, o): return nn.Sequential(nn.Conv2d(i, o, 3, 1, 1), nn.GroupNorm(8, o), nn.GELU(), nn.Conv2d(o, o, 3, 1, 1), nn.GroupNorm(8, o), nn.GELU())
        c = [base, base * 2, base * 4, base * 8]
        self.e0, self.e1, self.e2, self.mid = blk(n_in, c[0]), blk(c[0], c[1]), blk(c[1], c[2]), blk(c[2], c[3])
        self.u2, self.d2 = nn.ConvTranspose2d(c[3], c[2], 2, 2), blk(c[3], c[2])
        self.u1, self.d1 = nn.ConvTranspose2d(c[2], c[1], 2, 2), blk(c[2], c[1])
        self.u0, self.d0 = nn.ConvTranspose2d(c[1], c[0], 2, 2), blk(c[1], c[0])
        self.out = nn.Conv2d(c[0], n_out, 1)
    def forward(self, x):
        e0 = self.e0(x); e1 = self.e1(F.max_pool2d(e0, 2)); e2 = self.e2(F.max_pool2d(e1, 2)); h = self.mid(F.max_pool2d(e2, 2))
        h = self.d2(torch.cat([self.u2(h), e2], 1)); h = self.d1(torch.cat([self.u1(h), e1], 1)); h = self.d0(torch.cat([self.u0(h), e0], 1))
        return self.out(h)


class UBranch(nn.Module):
    """U-FNO 의 U 경로: 잠재 채널(width) 위의 작은 U-Net (2단 내려갔다 올라옴, GroupNorm·GELU). 입출력 채널 = width."""
    def __init__(self, w):
        super().__init__()
        def blk(i, o): return nn.Sequential(nn.Conv2d(i, o, 3, 1, 1), nn.GroupNorm(8, o), nn.GELU(), nn.Conv2d(o, o, 3, 1, 1), nn.GroupNorm(8, o), nn.GELU())
        self.e0, self.e1, self.mid = blk(w, w), blk(w, 2 * w), blk(2 * w, 4 * w)
        self.u1, self.d1 = nn.ConvTranspose2d(4 * w, 2 * w, 2, 2), blk(4 * w, 2 * w)
        self.u0, self.d0 = nn.ConvTranspose2d(2 * w, w, 2, 2), blk(2 * w, w)
        self.out = nn.Conv2d(w, w, 1)
    def forward(self, x):
        e0 = self.e0(x); e1 = self.e1(F.max_pool2d(e0, 2)); h = self.mid(F.max_pool2d(e1, 2))
        h = self.d1(torch.cat([self.u1(h), e1], 1)); h = self.d0(torch.cat([self.u0(h), e0], 1)); return self.out(h)


class UFourierLayer(nn.Module):
    """U-Fourier 층 (Wen et al. 2022, U-FNO): σ(K v + W v + U v) — 스펙트럼 합성곱 + 점별 1×1 + U-Net 경로(국소 구조)"""
    def __init__(self, w, modes):
        super().__init__(); self.spec = M.SpectralConv2d(w, w, modes, modes); self.lin = nn.Conv2d(w, w, 1); self.unet = UBranch(w)
    def forward(self, x): return F.gelu(self.spec(x) + self.lin(x) + self.unet(x))


class UFNO(nn.Module):
    """U-FNO 바람장 판 (2026-10-11): 올림(1×1) → Fourier 층 2 → U-Fourier 층 2 → 머리(Fourier 층 1 + 점별 MLP).
    FNO(DAFNO 단일 머리)와 같은 width 32·modes 24·층 수, 뒤쪽 두 층에만 U-Net 경로를 더했다 (원 논문: 앞 절반 Fourier, 뒤 절반 U-Fourier). 약 6.9 M."""
    def __init__(self, n_in=N_IN, n_out=N_OUT, width=32, modes=24):
        super().__init__()
        self.lift = nn.Conv2d(n_in, width, 1)
        self.layers = nn.ModuleList([M.FourierLayer(width, modes), M.FourierLayer(width, modes), UFourierLayer(width, modes), UFourierLayer(width, modes)])
        self.head = M.Head(width, modes, n_out, 1)
    def forward(self, x):
        h = self.lift(x)
        for l in self.layers: h = l(h)
        return self.head(h)


class WindNet(nn.Module):
    """arch: fno (농도 실험과 같은 FNO, 비주기 영역용 0-패딩 16칸) | ufno (FNO + U-Net 경로, 같은 패딩) | cnn (농도 실험의 CNN, 디코더 1개) | unet.
    출력 = 배경풍 + 보정. use_F: 입력 20채널(마지막 = F − 1)이고 기준을 '배경풍 × F'(수평 성분)로 둔다 — 막힘 가속을 기준에 반영."""
    def __init__(self, arch, width=32, modes=24, pad=16, use_F=False):
        super().__init__(); self.arch = arch; self.pad = pad if arch in ('fno', 'ufno') else 0; self.use_F = use_F; self.n_in = N_IN + int(use_F)
        if arch == 'fno': self.net = M.DAFNO(self.n_in, N_OUT, width, modes, single_head=True)
        elif arch == 'ufno': self.net = UFNO(self.n_in, N_OUT, width, modes)
        elif arch == 'cnn': self.net = M.CNNDA(self.n_in, N_OUT, single_head=True)
        elif arch == 'unet': self.net = UNet(n_in=self.n_in)
        else: raise ValueError(arch)
    def forward(self, x):
        H, W = x.shape[-2:]; z = F.pad(x, (0, self.pad, 0, self.pad)) if self.pad else x
        o = self.net(z); o = o[0] if isinstance(o, tuple) else o
        b = base_from_x(x)
        if self.use_F:
            Fm = 1.0 + x[:, N_IN:N_IN + 1]; b[:, Y_U] = b[:, Y_U] * Fm; b[:, Y_V] = b[:, Y_V] * Fm
        return o[..., :H, :W] + b


# ---------- 지표 ----------
def _nrm(t): return torch.linalg.vector_norm(t.flatten(1), dim=1)


@torch.no_grad()
def metrics(p, t, m6, x):
    """케이스별 지표 (B 개 dict). p, t: (B,18,H,W) /U_REF, m6: (B,6,H,W), x: 입력(배경풍 추출용).
    relL2_all: 18채널 상대 L2 | relL2_h{2,10,50}: 해당 층 수평 벡터 상대 L2 | skill: 1 − MSE/MSE_배경풍 (18채널, 1 = 완벽, 0 = 배경풍 수준)
    spd_mae10 [m/s], dir_mae10 [°] (CFD 풍속 > 0.5 m/s 셀), hit10: 10 m 풍속 |Δ| ≤ max(0.25·|U|, 0.25 m/s) 비율 (COST 732 식 hit rate), w_rmse [m/s]"""
    mm = m18(m6); b = base_from_x(x); e, eb = (p - t) * mm, (b - t) * mm
    out = dict(relL2_all=_nrm(e) / _nrm(t * mm).clamp_min(1e-8), skill=1 - _nrm(e) ** 2 / _nrm(eb) ** 2)
    for k, z in ((0, 2), (2, 10), (5, 50)):
        idx = [3 * k, 3 * k + 1]; out[f'relL2_h{z}'] = _nrm(e[:, idx]) / _nrm((t * mm)[:, idx]).clamp_min(1e-8)
    k = 2; mk = m6[:, k] > 0; U = G.U_REF
    sp, st = torch.hypot(p[:, 3 * k], p[:, 3 * k + 1]) * U, torch.hypot(t[:, 3 * k], t[:, 3 * k + 1]) * U
    out['spd_mae10'] = ((sp - st).abs() * mk).sum((1, 2)) / mk.sum((1, 2))
    ok = mk & (st > 0.5)
    ang = torch.rad2deg(torch.atan2(p[:, 3 * k + 1], p[:, 3 * k]) - torch.atan2(t[:, 3 * k + 1], t[:, 3 * k])); ang = (ang + 180) % 360 - 180
    out['dir_mae10'] = (ang.abs() * ok).sum((1, 2)) / ok.sum((1, 2)).clamp_min(1)
    out['hit10'] = (((sp - st).abs() <= torch.clamp(0.25 * st, min=0.25)) & mk).sum((1, 2)) / mk.sum((1, 2))
    mw = m6 > 0
    out['w_rmse'] = torch.sqrt((((p[:, Y_W] - t[:, Y_W]) * U) ** 2 * mw).sum((1, 2, 3)) / mw.sum((1, 2, 3)))
    return [{k: float(v[i]) for k, v in out.items()} for i in range(p.shape[0])]


def to_conc_wind(pw, bmask6):
    """예측 바람장 (B,18,H,W) /U_REF → 농도 모델 입력 18채널 (같은 배치·정규화), 건물 내부 0."""
    return pw * m18(bmask6)
