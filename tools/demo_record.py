"""시연 자동 진행 + 백업 영상 녹화 — 모니터 1(/ops)과 모니터 2(/insight)를 같은 시간축으로 동시에.
헤드리스 Chrome 2개(1920×1080)로 두 화면을 띄우고, 시연 대본(STORY)대로 버튼을 누르며 화면을 녹화한다 (NVENC H.264, 15 fps).
시나리오 대상(배출 급증 배출원)은 /api/meta 의 demo 설정에서 읽는다 (코드에 센서 코드를 두지 않음).

실행 (demo 에서, 시스템이 떠 있는 상태):  .conda\\python.exe tools\\demo_record.py <출력 폴더> [--only=ops|insight] [--no-record] [--limit=초]
  --only: 한 화면만 띄워 녹화하고, 다른 화면의 동작(배출 급증·경보 처리·민원)은 API 로 대신한다 (CPU 절약 — 화면 둘을 동시에 그리면 80 % 를 넘음)
필요: Chrome, imageio-ffmpeg (pip), NVIDIA GPU(없으면 libx264 로 대체)
"""
import asyncio, base64, json, os, subprocess, sys, time
import httpx, websockets

BASE = 'http://127.0.0.1:8000'
CHROME = next((p for p in [r'C:\Program Files\Google\Chrome\Application\chrome.exe', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'] if os.path.exists(p)), 'chrome')
FPS = 15
NTH = int(next((a.split('=')[1] for a in sys.argv if a.startswith('--nth=')), 4))
LIMIT = float(next((a.split('=')[1] for a in sys.argv if a.startswith('--limit=')), 0))   # 시험용: 이 시간(초) 뒤 녹화 종료
OUT = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else os.path.join(os.getcwd(), 'demo_video')
RECORD = '--no-record' not in sys.argv
ONLY = next((a.split('=')[1] for a in sys.argv if a.startswith('--only=')), None)
T0 = time.time()


def log(*a): print(f'[{time.time() - T0:6.1f}s]', *a, flush=True)


class Tab:
    """헤드리스 Chrome 한 개 + 페이지 한 개 (CDP)"""
    def __init__(self, name, port):
        self.name, self.port, self.n, self.wait, self.frame, self.events = name, port, 0, {}, None, []

    async def start(self, path):
        prof = os.path.join(os.environ.get('TEMP', '.'), f'odortwin-rec-{self.name}')
        self.proc = subprocess.Popen([CHROME, '--headless=new', f'--remote-debugging-port={self.port}', f'--user-data-dir={prof}', '--window-size=1920,1080',
                                      '--hide-scrollbars', '--force-device-scale-factor=1', '--no-first-run', '--enable-gpu', '--ignore-gpu-blocklist',
                                      '--autoplay-policy=no-user-gesture-required', '--mute-audio', 'about:blank'])
        for _ in range(60):
            try: tabs = httpx.get(f'http://127.0.0.1:{self.port}/json').json(); break
            except Exception: await asyncio.sleep(0.5)
        page = next(t for t in tabs if t['type'] == 'page')
        self.ws = await websockets.connect(page['webSocketDebuggerUrl'], max_size=128 * 1024 * 1024)
        self.reader = asyncio.create_task(self._read())
        await self.cmd('Page.enable')
        await self.cmd('Emulation.setDeviceMetricsOverride', width=1920, height=1080, deviceScaleFactor=1, mobile=False)
        await self.cmd('Page.navigate', url=BASE + path)

    async def _read(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if 'id' in m and m['id'] in self.wait: self.wait.pop(m['id']).set_result(m.get('result', m.get('error')))
            elif m.get('method') == 'Page.screencastFrame':
                p = m['params']; self.frame = base64.b64decode(p['data'])
                asyncio.create_task(self._ack(p['sessionId']))

    async def _ack(self, sid):
        try: await self.cmd('Page.screencastFrameAck', sessionId=sid)
        except Exception: pass                                         # 녹화를 끝낸 뒤 늦게 온 프레임은 무시

    async def cmd(self, method, **params):
        self.n += 1; i = self.n; fut = asyncio.get_event_loop().create_future(); self.wait[i] = fut
        await self.ws.send(json.dumps(dict(id=i, method=method, params=params)))
        return await asyncio.wait_for(fut, 60)

    async def js(self, expr):
        r = await self.cmd('Runtime.evaluate', expression=expr, returnByValue=True, awaitPromise=True)
        return (r or {}).get('result', {}).get('value')

    async def until(self, expr, timeout=40, every=0.5):
        t = time.time()
        while time.time() - t < timeout:
            v = await self.js(expr)
            if v: return v
            await asyncio.sleep(every)
        log(self.name, 'timeout waiting', expr[:80]); return None

    async def click_text(self, text):
        ok = await self.js(f"(() => {{ const b = [...document.querySelectorAll('button')].find(e => e.textContent.trim() === {json.dumps(text)} && !e.disabled); if (b) b.click(); return !!b }})()")
        log(self.name, 'click', text, '' if ok else '(없음)'); return ok

    async def caption(self, text):
        await self.js("""(t => { let d = document.getElementById('__cap'); if (!d) { d = document.createElement('div'); d.id = '__cap';
          d.style.cssText = 'position:fixed;left:50%;bottom:46px;transform:translateX(-50%);z-index:99;background:rgba(3,7,18,.86);color:#f8fafc;' +
            'border:1px solid #334155;border-radius:10px;padding:10px 22px;font:500 21px "Noto Sans KR",sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.5);white-space:nowrap';
          document.body.appendChild(d) } d.textContent = t; d.style.display = t ? 'block' : 'none' })(""" + json.dumps(text) + ')')

    async def stop(self):
        try: await self.ws.close()
        except Exception: pass
        self.proc.terminate()


class Stub:
    """녹화하지 않는 화면: 브라우저 없이 같은 시간표로 진행 (버튼 동작은 api 로)"""
    def __init__(self, name, api=None): self.name, self.api, self.frame = name, api or {}, None
    async def start(self, path): pass
    async def js(self, expr): return None
    async def until(self, expr, timeout=40, every=0.5): return True
    async def caption(self, text): pass
    async def stop(self): pass
    async def click_text(self, text):
        f = self.api.get(text)
        if f: f(); log(self.name, 'api', text)
        return True


class Recorder:
    """최신 화면 프레임을 일정 간격(FPS)으로 ffmpeg 에 넣는다 (화면이 안 바뀌면 같은 프레임 반복)"""
    def __init__(self, tab, path):
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        enc = ['-c:v', 'h264_nvenc', '-preset', 'p5', '-b:v', '8M'] if b'h264_nvenc' in subprocess.run([exe, '-hide_banner', '-encoders'], capture_output=True).stdout else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23']
        self.tab = tab; self.n = 0
        self.ff = subprocess.Popen([exe, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'image2pipe', '-framerate', str(FPS), '-c:v', 'mjpeg', '-i', '-',
                                    *enc, '-pix_fmt', 'yuv420p', '-movflags', '+faststart', path], stdin=subprocess.PIPE)

    async def run(self):
        await self.tab.cmd('Page.startScreencast', format='jpeg', quality=75, maxWidth=1920, maxHeight=1080, everyNthFrame=NTH)   # 60 fps 화면 중 NTH 장에 1장만 (CPU 절약)
        t0 = time.time()
        while not self.done:
            if self.tab.frame:
                self.ff.stdin.write(self.tab.frame); self.n += 1
            await asyncio.sleep(max(0, t0 + self.n / FPS - time.time()))

    done = False

    def close(self):
        self.done = True
        try: self.ff.stdin.close(); self.ff.wait(60)
        except Exception: pass


async def main():
    meta = httpx.get(BASE + '/api/meta', timeout=10).json()
    demo = meta.get('demo') or {}; code = demo.get('spike_code')
    if not code: sys.exit('data/demo_config.json 의 scenario.spike_code 가 필요합니다')
    farm = next(s['farm'] for s in meta['sources'] if s['code'] == code); story = demo.get('story_cluster', 'C01'); west = demo.get('west_cluster', 'C02')
    log('배출 급증 시연 대상', farm, story)
    # 준비: 시나리오 해제, 대상 경보 정리, ×60
    httpx.post(BASE + '/api/scenario', json={'cmd': 'clear'}); httpx.post(BASE + '/api/scenario', json={'cmd': 'speed', 'value': 60})
    for a in httpx.get(BASE + '/api/alarms').json():
        if a['code'] == code and a['state'] != 'cleared': httpx.post(f"{BASE}/api/alarms/{a['id']}/clear")

    post = lambda body: httpx.post(BASE + '/api/scenario', json=body, timeout=30)
    ins_api = {'배출 급증 실행': lambda: post({'cmd': 'spike', 'code': code, 'factor': 10, 'floor': 30, 'minutes': 600}),
               '민원 발생': lambda: post({'cmd': 'complaint', 'code': code, 'place': f'{farm} 인근 주민'})}
    ops = Tab('ops', 9361) if ONLY in (None, 'ops') else Stub('ops')
    ins = Tab('insight', 9362) if ONLY in (None, 'insight') else Stub('insight', ins_api)
    def my_alarms(): return [a for a in httpx.get(BASE + '/api/alarms').json() if a['code'] == code and a['kind'] == 'EMISSION_HIGH' and a['state'] != 'cleared']
    async def alarm_api(label):                                       # 관제 화면 없이 경보 처리 (확인 → 저감 조치 → 해제)
        ep = {'확인': 'ack', '저감 조치': 'action', '해제': 'clear'}.get(label)
        for a in my_alarms() if ep else []:
            httpx.post(f"{BASE}/api/alarms/{a['id']}/{ep}", json={} if ep == 'action' else None); log('ops api', label, a['id'])
    async def wait_alarm_api(timeout=60):
        t = time.time()
        while time.time() - t < timeout:
            if my_alarms(): return True
            await asyncio.sleep(1)
    await ops.start('/ops'); await ins.start('/insight')
    await ops.until("document.querySelectorAll('.crow').length > 0 && document.querySelector('.sys')?.textContent.includes('정상')", 90)
    await ins.until("document.querySelector('.sys')?.textContent.includes('정상')", 60)
    await asyncio.sleep(6)
    recs = []
    if RECORD:
        os.makedirs(OUT, exist_ok=True); stamp = time.strftime('%Y%m%d_%H%M')
        recs = [Recorder(t, os.path.join(OUT, f'{nm}_{stamp}.mp4')) for t, nm in ((ops, '모니터1_관제'), (ins, '모니터2_통계민원')) if isinstance(t, Tab)]
        tasks = [asyncio.create_task(r.run()) for r in recs]
    card = f"[...document.querySelectorAll('.alarm-card')].find(e => e.textContent.includes({json.dumps(farm)}) && e.textContent.includes('배출 이상'))"
    async def cards_btn(label):
        if isinstance(ops, Stub): return await alarm_api(label)
        return await ops.js(f"(() => {{ const c = {card}; const b = c && [...c.querySelectorAll('button')].find(e => e.textContent.trim() === {json.dumps(label)}); if (b) b.click(); return !!b }})()")
    if LIMIT: asyncio.get_event_loop().call_later(LIMIT, asyncio.current_task().cancel)
    try:
        # ① 정상 운영
        await ops.caption('① 정상 운영 — 제주 124개 센서 실측 자료 60배속 재생 · 20개 권역 확산을 실시간 계산')
        await ins.caption('시연 진행 · 민원 역추적 · 가상 실험 · 데이터 흐름(스트림 + 레이크하우스)')
        await ops.click_text('제주 전역'); await asyncio.sleep(16)
        await ops.js(f"(() => {{ const r = [...document.querySelectorAll('.crow')].find(e => e.textContent.includes({json.dumps(west)})); if (r) r.click(); return !!r }})()"); await asyncio.sleep(12)
        await ops.js(f"(() => {{ const r = [...document.querySelectorAll('.crow')].find(e => e.textContent.includes({json.dumps(story)})); if (r) r.click(); return !!r }})()"); await asyncio.sleep(10)
        # ② 배출 급증 → ③ 경보
        await ins.caption(f'② 배출 급증 — {farm} 배출구 NH₃ 급증 (시연 제어)'); await ops.caption(f'② {farm} 배출구 NH₃ 급증 → 5분 연속 기준 초과 감시 (Flink MATCH_RECOGNIZE)')
        await ins.click_text('배출 급증 실행')
        ok = await (ops.until(f"!!({card})", 60) if isinstance(ops, Tab) else wait_alarm_api(60))
        await ops.caption(f'③ 경보 발생 — 알림 · 자동 추적으로 {farm} 확대'); await asyncio.sleep(9)
        # ④ 확인 → 3D
        await ops.caption('④ 운영자 확인 → 3D 로 현장 확산 확인 (지형 · 건물 · 바람 유선)')
        await cards_btn('확인'); await asyncio.sleep(3); await cards_btn('3D'); await asyncio.sleep(16)
        await ops.caption('④ 바람 세기 — 오름·건물 주변에서 바람이 빨라지고(주황) 느려지는(파랑) 지형 효과')
        await ops.click_text('바람 세기'); await asyncio.sleep(14); await ops.click_text('바람 세기'); await asyncio.sleep(4)
        await ops.js("window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))"); await asyncio.sleep(3)
        # ⑤ 저감 조치 → ⑥ 해제
        await ops.caption('⑤ 저감 조치 — 게이트웨이에 MQTT 명령 → 저감시설 가동 → 농도 감소'); await cards_btn('저감 조치'); await asyncio.sleep(26)
        await ops.caption('⑥ 농도 정상화 확인 → 경보 해제'); await cards_btn('해제'); await asyncio.sleep(8)
        # ⑦ 민원 역추적
        await ops.caption('관제 화면은 계속 실시간으로 갱신'); await ins.caption('⑦ 민원 역추적 — 민원 지점의 예측 농도를 농가별로 분해해 원인 추정')
        await ins.click_text('민원 발생'); await asyncio.sleep(22)
        # ⑧ 데이터 흐름
        await ins.caption('⑧ 데이터 흐름 — MQTT → Kafka → Flink → GPU 확산 → 화면 · 배치: Airflow → Trino → Iceberg(bronze → silver → gold)'); await asyncio.sleep(16)
        # ⑨ 가상 실험
        await ins.caption('⑨ 가상 실험 — 바람이 바뀌면? GPU 대리모델 수 초 vs CFD 풍향 1개 35~45분')
        await ins.click_text('남서'); await ins.click_text('3 m/s'); await asyncio.sleep(2); await ins.click_text('GPU로 계산')
        await ins.until("document.body.innerText.includes('배 빠름') || document.body.innerText.includes('재사용')", 60); await asyncio.sleep(16)
        await ins.click_text('실시간으로 복귀'); await asyncio.sleep(4)
        # 마무리
        await ins.caption('통계 · 민원 · 시스템 — 레이크하우스 2년 기록과 실시간 스트림'); await ops.caption('실시간 관제 — 권역별 상태 · 확산 · 경보')
        await ops.click_text('제주 전역'); await asyncio.sleep(18)
        await ops.caption(''); await ins.caption(''); await asyncio.sleep(3)
    except asyncio.CancelledError:
        log('시험 시간 종료')
    finally:
        for r in recs: r.close()
        await ops.stop(); await ins.stop()
        httpx.post(BASE + '/api/scenario', json={'cmd': 'clear'})
        for r in recs: log('saved', r.ff.args[-1], f'{r.n / FPS:.0f} s', f'{os.path.getsize(r.ff.args[-1]) / 1e6:.0f} MB' if os.path.exists(r.ff.args[-1]) else '')


asyncio.run(main())
