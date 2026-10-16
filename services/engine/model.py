"""확산 대리모델 래퍼: 지형·건물·거칠기 + 풍향 → U-FNO+F 바람장(시드 3개 평균) → 가우시안 사전학습·CFD 미세조정·물리 잔차 보정 FNO 농도장(시드 3개 평균).
입력 구성은 연구 코드(services/engine/ml)를 그대로 쓰고, CFD 없는 농가는 래스터에서 만든다 (CFD 입력과 비교한 바람 예측 차 약 1.4 %).
출력: 단위배출(1 g/s)·기준 풍속 5 m/s 일 때의 2 m 농도 [s/m³] 와 10 m 바람 (u, v) [m/s].
풍향 흔들림 후처리(smooth_fields): 정상 상태 RANS 정답은 수 분 규모 풍향 변동을 담지 못해 측면 폭이 좁다 →
θ 주변 가우시안(σ_θ)으로 θ ± 3σ_θ 농도장을 가중 평균한다 (절대 2° 격자, README 모델 절 참고).
"""
import json, math, os, threading
from collections import OrderedDict
import numpy as np, torch
from PIL import Image
from .ml import wind as WD, models as M, data as D, gaussian as G
from services.common import config as C

MODELS = os.path.join(C.DEMO, 'models', 'runs')


class Surrogate:
    def __init__(self, device='cuda', cache_size=8000):
        self.dev = device if torch.cuda.is_available() else 'cpu'
        self.lock = threading.Lock()
        self.wnets = [self._load(WD.WindNet('ufno', use_F=True), f'{MODELS}/windF/ufno_all_s{s}/last.pt') for s in range(3)]
        # 농도 모델: 2026-10-16 이류-확산 물리 잔차(가변 D = 2.3e-5 + ν_t/0.7) 미세조정판. 입출력은 nut/ (10-14, 데이터 손실만) 과 같고
        # 정확도도 같으며 배출원 근처 피크 과대예측(1.18배 → 1.04배)이 줄고 잔차가 CFD 해 수준이 된다. 옛 pt_fno_n0 은 새 정답과 맞지 않아 쓰지 않음.
        self.cnets = [self._load(M.build('pt_fno', n_in=23, n_out=6), f'{MODELS}/nut_pino/pt_fno_n0_s{s}/best.pt') for s in range(3)]
        self.static, self.cache, self.cache_size = {}, OrderedDict(), cache_size
        self.mcache = OrderedDict()          # 풍향 흔들림 평균 결과 (code, θ, σ) → 2 m 농도

    def _load(self, net, path):
        net.load_state_dict(torch.load(path, map_location='cpu')); return net.to(self.dev).eval()

    def prepare(self, code):
        if code in self.static: return self.static[code]
        base = f'{C.DATA}/sites/{code}'
        site = json.load(open(f'{base}/site.json', encoding='utf-8'))
        zg = WD.read_tif(f'{base}/terrain_5m.tif').reshape(G.N, 2, G.N, 2).mean((1, 3))
        bh = WD.read_tif(f'{base}/building_height_5m.tif').reshape(G.N, 2, G.N, 2).max((1, 3))
        z0in = float(np.array(Image.open(f'{base}/z0_5m.tif'), dtype=np.float32).mean())
        st7 = WD.static_channels(C.DATA, code, zg, bh)
        sl = site['geometry']['sensor_local']
        st5 = D.static_channels(st7[0] * 50, st7[3] * 20, (sl['x'], sl['y']))
        b = site['bounds_epsg5186']
        self.static[code] = dict(st7=st7, z0in=z0in, st5=torch.from_numpy(st5).to(self.dev),
                                 bmask=torch.from_numpy(WD.bld_mask(bh)).to(self.dev), left=b['left'], bottom=b['bottom'], src=(sl['x'], sl['y']),
                                 zg=zg.astype(np.float32), bh=bh.astype(np.float32))
        return self.static[code]

    @torch.no_grad()
    def _forward(self, items):
        """→ 바람 (B, 18, H, W) [/U_REF], 농도 (B, 6, H, W) [s/m³] — 시드 3개씩 평균 (농도는 선형 공간에서 평균)"""
        xs = []
        for code, th in items:
            st = self.static[code]
            xs.append(np.concatenate([WD.background(th, st['z0in']), st['st7'], WD.cont_factor(st['st7'][0], th)[None] - 1.0]))
        x = torch.from_numpy(np.stack(xs).astype(np.float32)).to(self.dev)
        w = sum(n(x) for n in self.wnets) / len(self.wnets)
        bm = torch.stack([self.static[c]['bmask'] for c, _ in items]); st5 = torch.stack([self.static[c]['st5'] for c, _ in items])
        xc = torch.cat([WD.to_conc_wind(w, bm), st5], 1)
        c = sum(D.decode_target(n(xc, 'T')[0]).clamp_min(0) for n in self.cnets) / len(self.cnets)
        return w, c

    def _run(self, items):
        w, c = self._forward(items)
        u10 = torch.nn.functional.avg_pool2d(w[:, 6:7] * G.U_REF, 10)[:, 0]; v10 = torch.nn.functional.avg_pool2d(w[:, 7:8] * G.U_REF, 10)[:, 0]
        return c[:, 0].float().cpu().numpy(), u10.cpu().numpy().astype(np.float16), v10.cpu().numpy().astype(np.float16)

    @staticmethod
    def meander_plan(theta, sigma, step=2):
        """풍향 흔들림 평균에 쓸 각도와 가중치. 각도는 절대 격자(step° 배수)에서 θ ± 3σ 범위를 뽑는다
        → 바람이 조금 돌아도 대부분의 각도를 캐시에서 재사용한다 (σ = 3° 이면 11개, 6° 이면 19개). σ ≤ 0 이면 θ 하나."""
        th = int(round(theta)) % 360
        if sigma <= 0: return np.array([th]), np.array([1.0])
        K = int(math.ceil(3 * sigma / step)); base = step * int(round(th / step))
        angs = base + step * np.arange(-K, K + 1)
        w = np.exp(-0.5 * ((angs - theta) / sigma) ** 2); return angs % 360, w / w.sum()

    def smooth_fields(self, items, sigma):
        """풍향 흔들림 반영 단위 농도장. items: [(code, θ)] → {(code, θ정수): (평균 2 m 농도, 중심 풍향 u10, v10)}. σ = 0 이면 unit_fields 와 같다."""
        if sigma <= 0: return self.unit_fields(items)
        keys = list(dict.fromkeys((c, int(round(t)) % 360) for c, t in items)); out, need = {}, []
        for k in keys:
            mk = (k[0], k[1], sigma)
            if mk in self.mcache: self.mcache.move_to_end(mk); out[k] = self.mcache[mk]
            else: need.append(k)
        if need:
            plan = {k: self.meander_plan(k[1], sigma) for k in need}
            raw = self.unit_fields([(c, int(a)) for (c, th), (angs, _) in plan.items() for a in angs] + list(need))
            for (c, th), (angs, w) in plan.items():
                acc = np.zeros_like(raw[(c, th)][0])
                for a, ww in zip(angs, w): acc += raw[(c, int(a))][0] * ww
                self.mcache[(c, th, sigma)] = out[(c, th)] = (acc, raw[(c, th)][1], raw[(c, th)][2])
            while len(self.mcache) > 1500: self.mcache.popitem(last=False)
        return out

    def fields_3d(self, items, batch=8):
        """items: [(code, θ)] → 항목 순서대로 [(C (6 층 2·5·10·20·30·50 m, H, W) [s/m³], 바람 (18, H, W) [m/s, 기준 풍속 5 m/s])]. 캐시하지 않는다(농가 3D 보기용)."""
        for c, _ in items: self.prepare(c)
        res = []
        for i in range(0, len(items), batch):                  # 잠금은 묶음마다 → 실시간 루프·다른 요청이 사이사이 끼어들 수 있게
            with self.lock:
                w, c = self._forward(items[i:i + batch])
                w = (w * G.U_REF).float().cpu().numpy(); c = c.float().cpu().numpy()
            res += [(c[j], w[j]) for j in range(len(c))]
        return res

    def unit_fields(self, items, batch=8):
        """items: [(code, theta)] → {(code, θ정수): (C2m(200,200) s/m³, u10(20,20), v10(20,20))}. 풍향은 1° 단위로 캐시."""
        keys = [(c, int(round(t)) % 360) for c, t in items]; out, todo = {}, []
        for k in dict.fromkeys(keys):
            if k in self.cache: self.cache.move_to_end(k); out[k] = self.cache[k]
            else: todo.append(k)
        for k in todo: self.prepare(k[0])
        for i in range(0, len(todo), batch):                   # 잠금은 묶음마다 (한 권역 계산이 길어도 다른 요청이 끼어들 수 있게)
            part = todo[i:i + batch]
            with self.lock:
                c, u, v = self._run([(code, float(th)) for code, th in part])
            for j, k in enumerate(part):
                self.cache[k] = out[k] = (c[j], u[j], v[j])
        while len(self.cache) > self.cache_size: self.cache.popitem(last=False)
        return out
