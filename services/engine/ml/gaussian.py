"""소스 도메인: 가우시안 플룸 3D 해석해 (타겟 논문 Li et al. 2025 식 1; Pasquill–Gifford σ, Briggs 1973 open-country 식).
우리 격자(200×200, 10 m, AGL 6층)에 균일풍·평탄지형·단위배출 Q=1 로 생성. 입력 바람은 CFD 유입과 같은 로그 프로파일.
근거: σ_y, σ_z 는 하류거리·안정도만의 함수(Briggs 1973; Gifford 1961) → C ∝ Q/u 선형. 지면 반사항 포함(식 1).
"""
import math, numpy as np

LEVELS = np.array([2., 5., 10., 20., 30., 50.])          # AGL [m], 입력·출력 공통 6층
N = 200; DX = 10.0; L = N * DX                            # 격자
XS = (np.arange(N) + 0.5) * DX                            # 셀 중심 5..1995
U_REF, Z_REF, KAPPA = 5.0, 10.0, 0.41
Z0_IN = 0.5                                               # 유입 거칠기(학습 14농가 평균 z0 ≈0.46~0.92 → 대표 0.5)
# 체적 배출원 초기분산 (EPA ISC3 User's Guide 1995, Vol.II §1.2.2 volume source): σ_y0 = L/4.3, σ_z0 = H/2.15, L=H=30 m (CFD 15 m 구 배출원 지름)
# σ_eff = sqrt(σ(x)² + σ0²) → 점배출원 근접 특이점(1/σyσz→∞) 제거, 최대 농도 규모를 CFD(≤1.5e-3 s/m³)와 정합
SRC_D = 30.0; SY0 = SRC_D / 4.3; SZ0 = SRC_D / 2.15

# Briggs (1973) open-country dispersion coefficients, x [m]
def sigma_yz(x, stab):
    x = np.maximum(x, 0.0)
    if stab == 'A': sy = 0.22 * x * (1 + 1e-4 * x) ** -0.5; sz = 0.20 * x
    elif stab == 'B': sy = 0.16 * x * (1 + 1e-4 * x) ** -0.5; sz = 0.12 * x
    elif stab == 'C': sy = 0.11 * x * (1 + 1e-4 * x) ** -0.5; sz = 0.08 * x * (1 + 2e-4 * x) ** -0.5
    elif stab == 'D': sy = 0.08 * x * (1 + 1e-4 * x) ** -0.5; sz = 0.06 * x * (1 + 1.5e-3 * x) ** -0.5   # 논문과 동일
    elif stab == 'E': sy = 0.06 * x * (1 + 1e-4 * x) ** -0.5; sz = 0.03 * x * (1 + 3e-4 * x) ** -1
    elif stab == 'F': sy = 0.04 * x * (1 + 1e-4 * x) ** -0.5; sz = 0.016 * x * (1 + 3e-4 * x) ** -1
    else: raise ValueError(stab)
    return np.sqrt(sy ** 2 + SY0 ** 2), np.sqrt(sz ** 2 + SZ0 ** 2)


def log_profile(z):
    """중립 ABL 로그 법칙 (CFD atmBoundaryLayerInlet 과 동일 형태), U(Z_REF)=U_REF."""
    ustar = KAPPA * U_REF / np.log((Z_REF + Z0_IN) / Z0_IN)
    return ustar / KAPPA * np.log((z + Z0_IN) / Z0_IN)


def flow_dir(theta_deg):
    th = np.radians(theta_deg); return np.array([-np.sin(th), -np.cos(th)])   # 기상학적 풍향(불어오는 방향) → 유동 방향


def plume(theta_deg, stab='D', H=2.0, src=(L / 2, L / 2), Q=1.0, u=U_REF):
    """단위배출 농도 C(z_k, y, x) [s/m^3], shape (6, N, N). 식 1 + 좌표 회전."""
    f = flow_dir(theta_deg); n = np.array([-f[1], f[0]])
    X, Y = np.meshgrid(XS, XS)                            # (y, x) 배열, y 는 행
    dx, dy = X - src[0], Y - src[1]
    xp = dx * f[0] + dy * f[1]; yp = dx * n[0] + dy * n[1]
    sy, sz = sigma_yz(xp, stab)
    lat = np.exp(-yp ** 2 / (2 * sy ** 2)) / (2 * np.pi * sy * sz * u)
    out = np.zeros((len(LEVELS), N, N), dtype=np.float32)
    for k, z in enumerate(LEVELS):
        vert = np.exp(-(z - H) ** 2 / (2 * sz ** 2)) + np.exp(-(z + H) ** 2 / (2 * sz ** 2))
        c = Q * lat * vert; c[xp < 0] = 0.0
        out[k] = c
    return out


def uniform_wind(theta_deg):
    """균일 바람장 (3, 6, N, N): u,v 는 로그 프로파일 × 유동방향, w=0."""
    f = flow_dir(theta_deg); U = log_profile(LEVELS)       # (6,)
    u = np.broadcast_to((U * f[0])[:, None, None], (len(LEVELS), N, N)).astype(np.float32)
    v = np.broadcast_to((U * f[1])[:, None, None], (len(LEVELS), N, N)).astype(np.float32)
    w = np.zeros_like(u)
    return np.stack([u, v, w])


def source_blob(src=(L / 2, L / 2), sigma=15.0):
    """배출원 지시 채널 (N,N), 반경 15 m 구에 대응하는 가우시안 블롭, 합=1."""
    X, Y = np.meshgrid(XS, XS); s = np.exp(-((X - src[0]) ** 2 + (Y - src[1]) ** 2) / (2 * sigma ** 2)).astype(np.float32)
    return s / s.sum()


if __name__ == '__main__':
    # 자가 점검: 질량 보존(수평 적분 ∝ 1/u × 연직 분포), 방향 정합
    c = plume(270, 'D'); print('plume shape', c.shape, 'max %.2e at z=2' % c[0].max())
    X, Y = np.meshgrid(XS, XS); m = c[0] > 0
    print('플룸 무게중심 x=%.0f (서풍 270 → 동쪽 >1000 기대)' % ((X[m] * c[0][m]).sum() / c[0][m].sum()))
    print('log profile U(2,10,50)=', np.round(log_profile(np.array([2, 10, 50])), 2))


# ---------------- torch(GPU) 벡터화 버전: 학습 중 on-the-fly 소스 샘플 생성 (CPU 병목 제거) ----------------
import torch
_SY_A = [0.22, 0.16, 0.11, 0.08, 0.06, 0.04]


def _sigma_torch(x, stab):
    """x (B,N,N) 하류거리(≥1), stab (B,) long 0..5 → sy, sz. Briggs 1973 open-country."""
    a = torch.tensor(_SY_A, device=x.device)[stab][:, None, None]
    sy = a * x * (1 + 1e-4 * x) ** -0.5
    szs = torch.stack([0.20 * x, 0.12 * x, 0.08 * x * (1 + 2e-4 * x) ** -0.5, 0.06 * x * (1 + 1.5e-3 * x) ** -0.5,
                       0.03 * x * (1 + 3e-4 * x) ** -1, 0.016 * x * (1 + 3e-4 * x) ** -1], 0)      # (6,B,N,N)
    sz = szs[stab, torch.arange(x.shape[0], device=x.device)]
    return torch.sqrt(sy ** 2 + SY0 ** 2), torch.sqrt(sz ** 2 + SZ0 ** 2)


def plume_torch(theta, stab, H, Q=1.0, u=U_REF):
    """theta (B,) deg, stab (B,) long, H (B,) m → C (B,6,N,N) [s/m^3], 배출원 격자 중심. 식 1 과 동일(numpy plume 과 allclose)."""
    dev = theta.device; th = torch.deg2rad(theta); f0, f1 = -torch.sin(th), -torch.cos(th); n0, n1 = -f1, f0
    xs = torch.tensor(XS, device=dev, dtype=torch.float32); X, Y = torch.meshgrid(xs, xs, indexing='xy')
    dx, dy = (X - L / 2)[None], (Y - L / 2)[None]
    xp = dx * f0[:, None, None] + dy * f1[:, None, None]; yp = dx * n0[:, None, None] + dy * n1[:, None, None]
    sy, sz = _sigma_torch(xp.clamp_min(0.0), stab)
    lat = torch.exp(-yp ** 2 / (2 * sy ** 2)) / (2 * math.pi * sy * sz * u)
    z = torch.tensor(LEVELS, device=dev, dtype=torch.float32)[None, :, None, None]; Hh = H[:, None, None, None]; sz4 = sz[:, None]
    vert = torch.exp(-(z - Hh) ** 2 / (2 * sz4 ** 2)) + torch.exp(-(z + Hh) ** 2 / (2 * sz4 ** 2))
    C = Q * lat[:, None] * vert; C = torch.where((xp < 0)[:, None], torch.zeros_like(C), C)
    return C


def uniform_wind_torch(theta):
    """theta (B,) → (B,18,N,N) 채널 [u_k,v_k,w_k]×6 (m/s, 정규화 전)"""
    dev = theta.device; th = torch.deg2rad(theta); f0, f1 = -torch.sin(th), -torch.cos(th)
    U = torch.tensor(log_profile(LEVELS), device=dev, dtype=torch.float32)       # (6,)
    u = U[None] * f0[:, None]; v = U[None] * f1[:, None]; w = torch.zeros_like(u)   # (B,6)
    uvw = torch.stack([u, v, w], 2).reshape(theta.shape[0], 18)                    # [u0,v0,w0,u1,...]
    return uvw[:, :, None, None].expand(-1, -1, N, N)
