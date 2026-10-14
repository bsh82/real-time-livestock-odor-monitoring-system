"""D4 대칭 증강 (정사각 격자의 회전 4 + 반사 4). 지배방정식(RANS·수송)의 회전·반사 공변성 → 정확한 라벨.
배열 규약: 텐서 (..., y, x), y 가 행(아래→위로 증가), x 가 열. 수평 벡터 (u,v) 는 격자 재배열 + 성분 선형변환 R.
각 변환 g 를 2×2 행렬 R_g 로 정의하고, 격자 재배열은 "출력 위치 p' = R_g p (중심 기준)" 가 되도록 torch 연산을 매핑한다.
풍향(기상학적, 0=북·시계방향) 변환: 유동방향 벡터 f=(-sinθ,-cosθ) 에 R_g 를 적용한 뒤 θ' = atan2(-f'_x, -f'_y).
검증: 해석적 벡터장을 변환해 R_g v(R_g^{-1} p) 와 일치하는지 확인.
"""
import math, torch

# 8 변환: (이름, R 행렬, 격자 연산 함수)  — 격자 연산은 (…, y, x) 텐서에 적용
def _rot(k):  # torch.rot90(k, dims=(-2,-1)): out[i,j]=in[j,n-1-i] → 물리좌표(px=col,py=row)에서 시계방향 k×90°.
    return lambda t: torch.rot90(t, k, dims=(-2, -1))
def _flip_x(t): return torch.flip(t, dims=(-1,))          # x → -x (세로축 반사, 좌우)
def _flip_y(t): return torch.flip(t, dims=(-2,))          # y → -y (가로축 반사, 상하)
def _transpose(t): return t.transpose(-2, -1)             # (x,y) 교환 (대각선 반사)
def _antidiag(t): return torch.rot90(t.transpose(-2, -1), 2, dims=(-2, -1))  # 반대각선 반사

D4 = [
    ('id',    torch.tensor([[1., 0.], [0., 1.]]),   _rot(0)),
    ('rot90', torch.tensor([[0., -1.], [1., 0.]]),  _rot(3)),   # torch.rot90(k=1) 는 (y,x) 규약에서 시계방향 → 반시계 90° 는 k=3
    ('rot180',torch.tensor([[-1., 0.], [0., -1.]]), _rot(2)),
    ('rot270',torch.tensor([[0., 1.], [-1., 0.]]),  _rot(1)),
    ('flipx', torch.tensor([[-1., 0.], [0., 1.]]),  _flip_x),
    ('flipy', torch.tensor([[1., 0.], [0., -1.]]),  _flip_y),
    ('diag',  torch.tensor([[0., 1.], [1., 0.]]),   _transpose),
    ('adiag', torch.tensor([[0., -1.], [-1., 0.]]), _antidiag),
]


def apply(g, scalars=None, vectors=None, theta=None):
    """g: 0..7. scalars: 텐서 리스트 (…,y,x). vectors: (u,v) 쌍 리스트 [(u,v),…] 각 (…,y,x). theta: 풍향(도) 또는 None."""
    name, R, op = D4[g]
    out_s = [op(t) for t in scalars] if scalars else None
    out_v = None
    if vectors:
        out_v = []
        for u, v in vectors:
            u2 = R[0, 0] * u + R[0, 1] * v; v2 = R[1, 0] * u + R[1, 1] * v
            out_v.append((op(u2), op(v2)))
    th = None
    if theta is not None:
        t = math.radians(theta); f = torch.tensor([-math.sin(t), -math.cos(t)])
        f2 = R @ f; th = math.degrees(math.atan2(-f2[0].item(), -f2[1].item())) % 360.0
    return out_s, out_v, th


def apply_batch(g, x, y, u_idx, v_idx, theta=None):
    """학습 배치용: x (B,C,H,W) 입력, y (B,K,H,W) 출력. u_idx/v_idx: x 의 u,v 채널 인덱스 리스트(층별 쌍). 나머지 채널은 스칼라."""
    name, R, op = D4[g]
    x2 = x.clone()
    for ui, vi in zip(u_idx, v_idx):
        u, v = x[:, ui], x[:, vi]
        x2[:, ui] = R[0, 0] * u + R[0, 1] * v; x2[:, vi] = R[1, 0] * u + R[1, 1] * v
    x2 = op(x2); y2 = op(y)
    th = None
    if theta is not None:
        th = [apply(g, theta=float(t))[2] for t in theta]
    return x2, y2, th
