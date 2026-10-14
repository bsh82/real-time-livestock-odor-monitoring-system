"""모델.
- FNO2d 트렁크: Li et al. (2021, ICLR) Fourier Neural Operator. Lifting P → L× [SpectralConv2d(모드 절단) + 1×1 Conv] GELU → 잠재 h.
  기본값 width 32, modes 24, layers 4 (≈7 M; CNN 기준선 2.6 M 과 규모 근접, 2 km/24 모드 → 파장 ≥83 m 스펙트럴 해상) (Li et al. 2021 Navier-Stokes/Darcy 설정 범위: width 32–64, modes 12–32, 4층).
- DA-FNO: 공유 트렁크 + 도메인별 헤드 Q_S, Q_T (각 1 Fourier 층 + 점별 MLP). 타겟 논문의 '공유 FCN + 도메인별 디코더' 구조를 연산자로 치환.
- CNN-DA: 타겟 논문(Li et al. 2025) 재현 — 공유 인코더(여기선 conv, 입력이 이미지이므로 FCN 대신) + 두 transposed-conv 디코더. MMD 는 공유 특징에.
"""
import torch, torch.nn as nn, torch.nn.functional as F


# FNO 스펙트럴 합성곱 층: Li et al., "Fourier Neural Operator for Parametric PDEs" (ICLR 2021)
#   참고 구현 github.com/neuraloperator/neuraloperator (MIT License) 를 바탕으로 줄여 다시 씀
class SpectralConv2d(nn.Module):
    def __init__(self, cin, cout, m1, m2):
        super().__init__(); self.m1, self.m2 = m1, m2
        scale = 1 / (cin * cout)
        self.w1 = nn.Parameter(scale * torch.randn(cin, cout, m1, m2, dtype=torch.cfloat))
        self.w2 = nn.Parameter(scale * torch.randn(cin, cout, m1, m2, dtype=torch.cfloat))
    def forward(self, x):
        B, C, H, W = x.shape
        xf = torch.fft.rfft2(x, norm='ortho')
        out = torch.zeros(B, self.w1.shape[1], H, W // 2 + 1, dtype=torch.cfloat, device=x.device)
        out[:, :, :self.m1, :self.m2] = torch.einsum('bixy,ioxy->boxy', xf[:, :, :self.m1, :self.m2], self.w1)
        out[:, :, -self.m1:, :self.m2] = torch.einsum('bixy,ioxy->boxy', xf[:, :, -self.m1:, :self.m2], self.w2)
        return torch.fft.irfft2(out, s=(H, W), norm='ortho')


class FourierLayer(nn.Module):
    def __init__(self, width, modes):
        super().__init__(); self.spec = SpectralConv2d(width, width, modes, modes); self.lin = nn.Conv2d(width, width, 1)
    def forward(self, x): return F.gelu(self.spec(x) + self.lin(x))


class Head(nn.Module):
    def __init__(self, width, modes, n_out, n_fourier=1):
        super().__init__()
        self.layers = nn.ModuleList([FourierLayer(width, modes) for _ in range(n_fourier)])
        self.proj = nn.Sequential(nn.Conv2d(width, 128, 1), nn.GELU(), nn.Conv2d(128, n_out, 1))
    def forward(self, h):
        for l in self.layers: h = l(h)
        return self.proj(h)


class DAFNO(nn.Module):
    """공유 FNO 트렁크 + 도메인 헤드. forward(x, domain) → (pred, latent)"""
    def __init__(self, n_in=23, n_out=6, width=32, modes=24, n_shared=4, n_head=1, single_head=False):
        super().__init__()
        self.lift = nn.Conv2d(n_in, width, 1)
        self.trunk = nn.ModuleList([FourierLayer(width, modes) for _ in range(n_shared)])
        self.head_T = Head(width, modes, n_out, n_head)
        self.head_S = self.head_T if single_head else Head(width, modes, n_out, n_head)
    def latent(self, x):
        h = self.lift(x)
        for l in self.trunk: h = l(h)
        return h
    def forward(self, x, domain='T'):
        h = self.latent(x); return (self.head_T if domain == 'T' else self.head_S)(h), h
    def init_T_from_S(self):
        if self.head_T is not self.head_S: self.head_T.load_state_dict(self.head_S.state_dict())


class CNNDA(nn.Module):
    """논문 CNN 구조의 이미지 입력판: 공유 conv 인코더(4× 다운) → 공유 특징 → 도메인별 transposed-conv 디코더."""
    def __init__(self, n_in=23, n_out=6, base=32, single_head=False):
        super().__init__()
        def blk(i, o): return nn.Sequential(nn.Conv2d(i, o, 3, 2, 1), nn.BatchNorm2d(o), nn.ReLU(), nn.Conv2d(o, o, 3, 1, 1), nn.BatchNorm2d(o), nn.ReLU())
        self.enc = nn.Sequential(blk(n_in, base), blk(base, base * 2), blk(base * 2, base * 4), blk(base * 4, base * 8))  # 200→13 (ceil)
        def dec():
            def up(i, o): return nn.Sequential(nn.ConvTranspose2d(i, o, 4, 2, 1), nn.BatchNorm2d(o), nn.ReLU())
            return nn.Sequential(up(base * 8, base * 4), up(base * 4, base * 2), up(base * 2, base), up(base, base), nn.Conv2d(base, n_out, 3, 1, 1))
        self.dec_T = dec(); self.dec_S = self.dec_T if single_head else dec()
    def latent(self, x): return self.enc(x)
    def init_T_from_S(self):
        if self.dec_T is not self.dec_S: self.dec_T.load_state_dict(self.dec_S.state_dict())
    def forward(self, x, domain='T'):
        h = self.enc(x); y = (self.dec_T if domain == 'T' else self.dec_S)(h)
        y = F.interpolate(y, size=x.shape[-2:], mode='bilinear', align_corners=False)   # 200 복원
        return y, h


def pool_latent(h):
    """잠재 함수 → 벡터 (GAP ‖ GMP), MMD 용"""
    return torch.cat([h.mean(dim=(-2, -1)), h.amax(dim=(-2, -1))], dim=1)


# ---- 6절차 체계 (2026-10-04): 메서드 이름 = <protocol>_<arch>, 미세조정 = ft0_<base> (λ3=0) / pinn_<base> (λ3>0)
#  미세조정 접두어: ft0(물리항 없음) | pinn(v1 잔차) | pinnR(v2 잔차) | pinnF(질량 보존) | pinnRF(v2 잔차+질량 보존)
#  protocol: paper(논문 그대로: 사전학습 없음, 이중 디코더, 공동 학습, λ1=λ2=1e-4) | pt(사전학습→CFD만 미세조정, 단일 디코더) | prepaper(사전학습→공동 학습)
PROTO = dict(paper=dict(pretrain=False, single=False, lam1=1e-4, lam2=1e-4, use_S=True),
             pt=dict(pretrain=True, single=True, lam1=0.0, lam2=0.0, use_S=False),
             prepaper=dict(pretrain=True, single=False, lam1=1e-4, lam2=1e-4, use_S=True),
             cfd=dict(pretrain=False, single=True, lam1=0.0, lam2=0.0, use_S=False))        # CFD 만 일반 학습 (사전학습·소스·MMD 없음)
def parse6(name):
    """'pinn_prepaper_fno' → dict(ft='pinn', protocol='prepaper', arch='fno', **PROTO[...]) ; 구 메서드면 None"""
    p = name.split('_'); ft = None
    if p[0] in ('ft0', 'pinn', 'pinnR', 'pinnF', 'pinnRF'): ft = p[0]; p = p[1:]   # pinnR/F/RF = 물리 제약 변형
    if len(p) != 2 or p[0] not in PROTO or p[1] not in ('fno', 'cnn'): return None
    return dict(ft=ft, protocol=p[0], arch=p[1], **PROTO[p[0]])
def build_arch(arch, single_head, n_in=23, n_out=6, width=32, modes=24):
    return DAFNO(n_in, n_out, width, modes, single_head=single_head) if arch == 'fno' else CNNDA(n_in, n_out, single_head=single_head)

def build(name, **kw):
    c = parse6(name)
    if c: return build_arch(c['arch'], c['single'], **{k: v for k, v in kw.items() if k in ('n_in', 'n_out', 'width', 'modes')})
    if name == 'ptfno': return DAFNO(single_head=True, **kw)      # 사전학습→미세조정: 헤드 공유(트렁크+헤드 모두 전이), MMD 없음
    if name in ('dafno', 'cfdfno', 'pino'): return DAFNO(**kw)   # kw 에 single_head=True 가능 (헤드 공유 변형)
    if name == 'cnnda': return CNNDA(n_in=kw.get('n_in', 23), n_out=kw.get('n_out', 6))
    raise ValueError(name)
