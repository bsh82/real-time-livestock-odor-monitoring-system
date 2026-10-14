"""시각화 공통: 로그 연속 색상표(0.0005 ~ 3 ppm), 0.6·1 ppm 등치선, 계산 영역 가장자리 감쇠, 음영기복.
화면(web/src/colormap.ts)과 같은 색 정지점을 쓴다 — 값을 바꾸면 두 곳을 같이 바꿀 것.
"""
import numpy as np

# (ppm, R, G, B, alpha)
STOPS = [(0.0005, 30, 64, 160, 34), (0.002, 37, 99, 235, 78), (0.008, 6, 182, 212, 112), (0.03, 34, 197, 94, 140),
         (0.1, 234, 179, 8, 165), (0.3, 249, 115, 22, 190), (0.6, 239, 68, 68, 205), (1.0, 220, 38, 38, 220), (3.0, 192, 38, 211, 235)]
LOG0, LOG1 = np.log10(STOPS[0][0]), np.log10(STOPS[-1][0])
_lp = np.log10([s[0] for s in STOPS])
_t = np.linspace(LOG0, LOG1, 256)
LUT = np.stack([np.interp(_t, _lp, [s[k] for s in STOPS]) for k in range(1, 5)], 1).round().astype(np.uint8)   # (256, 4)
CONTOURS = [(0.6, (255, 237, 213, 255)), (1.0, (254, 226, 226, 255))]


def colorize(ppm):
    """ppm (H, W) → RGBA (H, W, 4). 0.0005 ppm 미만은 투명."""
    v = np.log10(np.maximum(ppm, 1e-12))
    idx = np.clip((v - LOG0) / (LOG1 - LOG0) * 255, 0, 255).astype(np.int32)
    rgba = LUT[idx].copy(); rgba[ppm < STOPS[0][0]] = 0
    for thr, col in CONTOURS:                                          # 등치선: 기준 이상인 칸 중 이웃이 기준 미만인 칸
        m = ppm >= thr
        if not m.any(): continue
        inner = m.copy()
        inner[1:, :] &= m[:-1, :]; inner[:-1, :] &= m[1:, :]; inner[:, 1:] &= m[:, :-1]; inner[:, :-1] &= m[:, 1:]
        rgba[m & ~inner] = col
    return rgba


def taper(n=200, w=15):
    """계산 영역(2 km) 가장자리 w 칸에서 0 → 1 로 부드럽게 올라가는 창. 낮은 농도까지 그릴 때 영역 경계가 잘린 선처럼 보이지 않게 한다(표시용)."""
    i = np.arange(n); d = np.minimum(i, n - 1 - i)
    t = 0.5 - 0.5 * np.cos(np.pi * np.clip(d / w, 0, 1))
    return np.outer(t, t).astype(np.float32)


def radial_window(n=200, dx=10.0, r0=650.0, r1=950.0):
    """배출원(영역 중심) 기준 원형 창: r < r0 는 1, r0~r1 코사인 감쇠, r1 밖은 0 (표시용).
    정사각 영역 경계에서 낮은 농도 꼬리가 잘린 모양이 되지 않게 한다. 통계·기여도에는 쓰지 않는다."""
    c = (np.arange(n) + 0.5) * dx; r = np.hypot(c[None, :] - n * dx / 2, c[:, None] - n * dx / 2)
    return (0.5 + 0.5 * np.cos(np.pi * np.clip((r - r0) / (r1 - r0), 0, 1))).astype(np.float32)


def hillshade(z, dx, az=315.0, alt=45.0):
    """z (행 = 남→북) → 0..1 음영 (북서쪽 조명)"""
    gy, gx = np.gradient(z, dx)
    n = np.stack([-gx, -gy, np.ones_like(z)]); n /= np.linalg.norm(n, axis=0)
    a, h = np.radians(az), np.radians(alt)
    L = np.array([np.sin(a) * np.cos(h), np.cos(a) * np.cos(h), np.sin(h)])
    return np.clip(np.tensordot(L, n, 1), 0, 1)
