"""OdorTwin API 서버 (FastAPI, :8000).
실시간: Kafka(odor.clean·odor.alert·odor.field·device.status·cmd.ack·sim.clock·ops.metrics·weather.wind) → WebSocket /ws 로 0.5초마다 묶어서 푸시
경보: 발생 → 확인(ack) → 조치(원격 명령, MQTT 하향) → 해제(clear). 같은 센서·종류는 하나로 묶고, 조건이 사라지면 자동 해제
민원: 위치를 받으면 확산 엔진에 농가별 기여도를 물어 저장
통계: 레이크하우스(Trino → Iceberg gold) 조회
실행: python -m services.api.main
"""
import asyncio, json, math, os, sys, threading, time, uuid
from collections import deque
import httpx, pandas as pd, psycopg
from psycopg.rows import dict_row
import trino
from confluent_kafka import Consumer, Producer
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from services.common import config as C

app = FastAPI(title='OdorTwin API')
FIELDS = os.path.join(C.RUNTIME, 'fields'); os.makedirs(FIELDS, exist_ok=True)
app.mount('/fields', StaticFiles(directory=FIELDS), name='fields')

S = pd.read_csv(os.path.join(C.DATA, 'sources.csv'), encoding='utf-8-sig', dtype={'code': str, 'research': str}, keep_default_na=False, na_values=[''])
S = S.astype(object).where(pd.notna(S), None)          # NaN → null (JSON)
S['research'] = S.research.fillna('')
SRC = {r.code: r._asdict() for r in S.itertuples(index=False)}
AUTO_CLEAR_MS = {'EMISSION_HIGH': 30 * 60 * 1000, 'IMPACT': 30 * 60 * 1000, 'SENSOR_RANGE': 6 * 3600 * 1000, 'SENSOR_STUCK': 6 * 3600 * 1000}  # 데이터 시각 기준 자동 해제
OPEN = ('active', 'ack', 'action')


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.sensors, self.fields, self.wind, self.status = {}, {}, {}, {}
        self.clock = dict(replay_ms=0, speed=C.REPLAY_SPEED, paused=False)
        self.pending = {}; self.events = deque(maxlen=500)
        self.rate = deque(maxlen=20); self.cnt = 0; self.lat = deque(maxlen=400)
        self.bridge = {}; self.engine = {}; self.last_kafka = 0.0


ST = State()
PROD = Producer({'bootstrap.servers': C.KAFKA, 'linger.ms': 5})
_pg_lock = threading.Lock(); _pg = None


def db():
    global _pg
    if _pg is None or _pg.closed: _pg = psycopg.connect(C.PG_DSN, autocommit=True, row_factory=dict_row)
    return _pg


def q(sql, args=None, one=False):
    with _pg_lock:
        for k in range(2):
            try:
                cur = db().execute(sql, args or ())
                if cur.description is None: return None
                return cur.fetchone() if one else cur.fetchall()
            except psycopg.OperationalError:
                global _pg; _pg = None
                if k: raise


def emit(kind, data):
    with ST.lock: ST.events.append(dict(type=kind, data=data))


# ---------------- 경보 ----------------
def raise_alarm(code, kind, level, ts_ms, value, detail, cluster=None):
    cluster = cluster or SRC.get(code, {}).get('cluster') or code
    row = q("SELECT * FROM alarms WHERE code=%s AND kind=%s AND state IN ('active','ack','action') ORDER BY id DESC LIMIT 1", (code, kind), one=True)
    if row:
        lvl = 'warning' if 'warning' in (row['level'], level) else level
        q('UPDATE alarms SET last_ts=to_timestamp(%s), value=GREATEST(value, %s), detail=%s, level=%s WHERE id=%s',
          (ts_ms / 1000, value, detail, lvl, row['id']))
        row = q('SELECT * FROM alarms WHERE id=%s', (row['id'],), one=True); emit('alarm', dict(event='update', alarm=jsonable(row)))
        return row
    recent = q("SELECT id FROM alarms WHERE code=%s AND kind=%s AND state='cleared' AND last_ts > to_timestamp(%s) - interval '15 minutes' ORDER BY id DESC LIMIT 1",
               (code, kind, ts_ms / 1000), one=True)
    if recent:                                                         # 방금 해제한 경보의 꼬리 → 기록만 갱신, 새 경보·알림 없음
        q('UPDATE alarms SET last_ts=to_timestamp(%s) WHERE id=%s', (ts_ms / 1000, recent['id'])); return recent
    row = q("INSERT INTO alarms (code, cluster, kind, level, first_ts, last_ts, value, detail) VALUES (%s,%s,%s,%s,to_timestamp(%s),to_timestamp(%s),%s,%s) RETURNING *",
            (code, cluster, kind, level, ts_ms / 1000, ts_ms / 1000, value, detail), one=True)
    q("INSERT INTO alarm_log (alarm_id, event, actor, note) VALUES (%s,'raised','system',%s)", (row['id'], detail))
    emit('alarm', dict(event='new', alarm=jsonable(row))); return row


def set_state(aid, state, actor='운영자', note=None, **cols):
    sets = ', '.join([f'{k}=%s' for k in cols] + ['state=%s'])
    q(f'UPDATE alarms SET {sets} WHERE id=%s', (*cols.values(), state, aid))
    q('INSERT INTO alarm_log (alarm_id, event, actor, note) VALUES (%s,%s,%s,%s)', (aid, state, actor, note))
    row = q('SELECT * FROM alarms WHERE id=%s', (aid,), one=True); emit('alarm', dict(event=state, alarm=jsonable(row))); return row


def jsonable(row):
    return {k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in (row or {}).items()}


def auto_clear():
    now = ST.clock['replay_ms']
    if not now: return
    for r in q("SELECT id, kind, state, extract(epoch from last_ts)*1000 AS last FROM alarms WHERE state IN ('active','ack','action') AND kind IN ('EMISSION_HIGH','IMPACT','SENSOR_RANGE','SENSOR_STUCK')") or []:
        lim = AUTO_CLEAR_MS.get(r['kind'], 30 * 60 * 1000)
        if r['state'] != 'active': lim = max(lim, 3 * 3600 * 1000)          # 확인·조치 중인 경보는 운영자가 해제할 때까지 (최대 3 데이터시간)
        if now - float(r['last']) > lim:
            set_state(r['id'], 'cleared', actor='system', note='조건 해소 자동 해제', cleared_at=pd.Timestamp.now(tz='Asia/Seoul').to_pydatetime())


# ---------------- Kafka 소비 ----------------
def consume():
    cons = Consumer({'bootstrap.servers': C.KAFKA, 'group.id': f'api-{uuid.uuid4().hex[:6]}', 'auto.offset.reset': 'latest'})
    cons.subscribe([C.TOPIC[k] for k in ('clean', 'alert', 'field', 'status', 'cmd_ack', 'clock', 'wind')] + ['ops.metrics'])
    while True:
        m = cons.poll(0.2)
        if m is None or m.error(): continue
        t = m.topic(); d = json.loads(m.value()); ST.last_kafka = time.time()
        try:
            if t == C.TOPIC['clean']:
                now = time.time() * 1000
                with ST.lock:
                    ST.sensors[d['code']] = d; ST.pending[d['code']] = d; ST.cnt += 1
                    if d.get('sent_ms'): ST.lat.append(now - d['sent_ms'])
            elif t == C.TOPIC['field']:
                with ST.lock: ST.fields[d['cluster']] = d
                emit('field', d)
            elif t == C.TOPIC['alert']:
                raise_alarm(d['code'], d['kind'], d['level'], d['ts_ms'], d.get('value') or 0, d.get('detail'), d.get('cluster'))
            elif t == C.TOPIC['status']:
                code = d['code']; ST.status[code] = d['status']; emit('status', d)
                if d['status'] == 'offline':
                    raise_alarm(code, 'COMM_LOSS', 'fault', ST.clock['replay_ms'], 0, '게이트웨이 통신 두절 (MQTT LWT 수신)')
                else:
                    for r in q("SELECT id FROM alarms WHERE code=%s AND kind='COMM_LOSS' AND state IN ('active','ack','action')", (code,)) or []:
                        set_state(r['id'], 'cleared', actor='system', note='통신 복구', cleared_at=pd.Timestamp.now(tz='Asia/Seoul').to_pydatetime())
            elif t == C.TOPIC['cmd_ack']:
                emit('cmd_ack', d)
                for r in q("SELECT id FROM alarms WHERE code=%s AND state='action'", (d['code'],)) or []:
                    q("INSERT INTO alarm_log (alarm_id, event, actor, note) VALUES (%s,'device_ack',%s,%s)", (r['id'], f"gw-{d['code']}", d.get('note') or d.get('status')))
            elif t == C.TOPIC['clock']:
                ST.clock = d
            elif t == C.TOPIC['wind']:
                ST.wind[d['key']] = d
            elif t == 'ops.metrics':
                ST.bridge = d
        except Exception as e:
            print('consume error', t, repr(e), flush=True)


async def pusher():
    last_clear = 0.0
    while True:
        await asyncio.sleep(0.5)
        with ST.lock:
            items, ST.pending = list(ST.pending.values()), {}; events = list(ST.events); ST.events.clear()
            ST.rate.append((time.time(), ST.cnt))
        msgs = [dict(type='sensors', items=items)] if items else []
        msgs += events; msgs.append(dict(type='kpi', data=kpi()))
        if time.time() - last_clear > 2:
            last_clear = time.time(); await asyncio.to_thread(auto_clear)
        dead = []
        for ws in list(CLIENTS):
            try:
                for mm in msgs: await ws.send_text(json.dumps(mm, ensure_ascii=False, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead: CLIENTS.discard(ws)


def kpi():
    r = list(ST.rate); rate = (r[-1][1] - r[0][1]) / max(r[-1][0] - r[0][0], 1e-6) if len(r) > 1 else 0
    now = ST.clock.get('replay_ms', 0); live = sum(1 for s in ST.sensors.values() if now - s['ts_ms'] < 5 * 60 * 1000)
    lat = sorted(ST.lat); p50 = lat[len(lat) // 2] if lat else None
    return dict(clock=ST.clock, msgs_per_s=round(rate, 1), latency_ms=round(p50, 0) if p50 else None, sensors_live=live, sensors_total=len(SRC),
                gateways=ST.clock.get('gateways'), bridge=ST.bridge, engine=ST.engine, kafka_ok=time.time() - ST.last_kafka < 5)


async def engine_poll():
    async with httpx.AsyncClient(timeout=3) as cli:
        while True:
            try: ST.engine = (await cli.get(f'{C.ENGINE_URL}/status')).json()
            except Exception: ST.engine = dict(error='engine unreachable')
            await asyncio.sleep(3)


CLIENTS = set()


@app.on_event('startup')
async def startup():
    threading.Thread(target=consume, daemon=True).start()
    asyncio.create_task(pusher()); asyncio.create_task(engine_poll())


@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    from urllib.parse import urlparse
    o = ws.headers.get('origin')                                     # 다른 사이트의 페이지가 실시간 자료를 받아 가지 못하게 (같은 출처·루프백만)
    if o and urlparse(o).hostname not in ('127.0.0.1', 'localhost', '::1', (ws.headers.get('host') or '').rsplit(':', 1)[0].strip('[]')):
        await ws.close(code=1008); return
    await ws.accept(); CLIENTS.add(ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        CLIENTS.discard(ws)


# ---------------- REST ----------------
@app.get('/api/meta')
def meta():
    try: clusters = httpx.get(f'{C.ENGINE_URL}/clusters', timeout=5).json()
    except Exception: clusters = []
    return dict(sources=[{**s, 'suspect': s['code'] in C.SUSPECT, 'suspect_reason': C.SUSPECT.get(s['code'])} for s in SRC.values()], clusters=clusters, thresholds=C.PPM_ALERT, demo=C.SCENARIO,
                notes=dict(sensor='제주 악취센서 124개소 실측(2023-02~2025-04) 재생', emission='배출량 = NH3 × 환기량(가정값)',
                           model='U-FNO+F 바람장 + FNO 농도장 (난류 확산 D = ν_t/0.7 CFD 학습, 시험 농가 로그 relL2 0.33·R² 0.70), 풍향 변동 σθ 후처리'))


@app.get('/api/state')
def state():
    with ST.lock: sensors = dict(ST.sensors); fields = dict(ST.fields); status = dict(ST.status); wind = dict(ST.wind)
    alarms = [jsonable(r) for r in q("SELECT * FROM alarms WHERE state IN ('active','ack','action') ORDER BY id DESC LIMIT 100") or []]
    comps = [jsonable(r) for r in q('SELECT * FROM complaints ORDER BY id DESC LIMIT 30') or []]
    return dict(sensors=sensors, fields=fields, status=status, wind=wind, alarms=alarms, complaints=comps, kpi=kpi())


@app.get('/api/alarms')
def alarms(state: str = 'open', limit: int = 100):
    if state == 'open': rows = q("SELECT * FROM alarms WHERE state IN ('active','ack','action') ORDER BY id DESC LIMIT %s", (limit,))
    else: rows = q('SELECT * FROM alarms ORDER BY id DESC LIMIT %s', (limit,))
    return [jsonable(r) for r in rows or []]


@app.get('/api/alarms/{aid}/log')
def alarm_log(aid: int): return [jsonable(r) for r in q('SELECT * FROM alarm_log WHERE alarm_id=%s ORDER BY id', (aid,)) or []]


@app.post('/api/alarms/{aid}/ack')
def ack(aid: int, actor: str = '운영자'):
    return jsonable(set_state(aid, 'ack', actor, '경보 확인', ack_by=actor, ack_at=pd.Timestamp.now(tz='Asia/Seoul').to_pydatetime()))


@app.post('/api/alarms/{aid}/action')
def action(aid: int, body: dict = Body(default={})):
    row = q('SELECT * FROM alarms WHERE id=%s', (aid,), one=True)
    if not row: raise HTTPException(404)
    target = body.get('code') or row['code']
    if target in SRC and SRC[target]['role'] == 'measure':           # 경계 센서 경보 → 같은 농가 배출원
        same = [c for c, s in SRC.items() if s['farm'] == SRC[target]['farm'] and s['role'] != 'measure']
        if same: target = same[0]
    if target not in SRC or SRC[target]['role'] == 'measure':       # 군집 경보면 기여도가 가장 큰 배출원에 명령
        f = ST.fields.get(row['cluster']) or {}
        mem = sorted(f.get('members', []), key=lambda m: -m['q'])
        target = mem[0]['code'] if mem else target
    cmd = dict(code=target, cmd_id=uuid.uuid4().hex[:8], action='mitigate', hours=6, by='운영자')
    PROD.produce(C.TOPIC['cmd_down'], json.dumps(cmd, ensure_ascii=False), key=target); PROD.flush(2)
    return jsonable(set_state(aid, 'action', '운영자', f"저감시설 가동 명령 → {SRC.get(target, {}).get('farm', target)} ({target})",
                              action=f"mitigate:{target}", action_at=pd.Timestamp.now(tz='Asia/Seoul').to_pydatetime()))


@app.post('/api/alarms/{aid}/clear')
def clear(aid: int, actor: str = '운영자'):
    return jsonable(set_state(aid, 'cleared', actor, '운영자 해제', cleared_at=pd.Timestamp.now(tz='Asia/Seoul').to_pydatetime()))


@app.get('/api/history/{code}')
def history(code: str, hours: float = 24):
    end = ST.clock.get('replay_ms') or 0
    rows = q('SELECT window_start, nh3_avg, nh3_max, temp_avg FROM sensor_agg10m WHERE code=%s AND window_start > to_timestamp(%s) ORDER BY window_start',
             (code, end / 1000 - hours * 3600))
    return [jsonable(r) for r in rows or []]


@app.get('/api/dispersion/{cluster}')
def dispersion_hist(cluster: str, hours: float = 24):
    end = ST.clock.get('replay_ms') or 0
    rows = q('SELECT * FROM dispersion WHERE cluster=%s AND ts > to_timestamp(%s) ORDER BY ts', (cluster, end / 1000 - hours * 3600))
    return [jsonable(r) for r in rows or []]


@app.post('/api/complaints')
def complaint(body: dict = Body(...)):
    lat, lon = float(body['lat']), float(body['lon'])
    try: att = httpx.get(f'{C.ENGINE_URL}/attribute', params=dict(lat=lat, lon=lon), timeout=5).json()
    except Exception as e: att = dict(error=str(e), sources=[])
    row = q('INSERT INTO complaints (ts, lat, lon, place, body, intensity, attribution) VALUES (to_timestamp(%s),%s,%s,%s,%s,%s,%s) RETURNING *',
            ((ST.clock.get('replay_ms') or time.time() * 1000) / 1000, lat, lon, body.get('place'), body.get('body', '악취 민원'),
             int(body.get('intensity', 3)), json.dumps(att, ensure_ascii=False)), one=True)
    row = jsonable(row); emit('complaint', row); return row


@app.get('/api/complaints')
def complaints(limit: int = 50): return [jsonable(r) for r in q('SELECT * FROM complaints ORDER BY id DESC LIMIT %s', (limit,)) or []]


@app.post('/api/complaints/{cid}/status')
def complaint_status(cid: int, status: str):
    row = jsonable(q('UPDATE complaints SET status=%s WHERE id=%s RETURNING *', (status, cid), one=True)); emit('complaint', row); return row


@app.post('/api/scenario')
def scenario(body: dict = Body(...)):
    """발표자 제어: speed/pause/resume/spike/stuck/offline/clear → 시뮬레이터, complaint → 확산 최대 지점 부근 민원 생성"""
    if body.get('cmd') == 'complaint':
        code = body.get('code'); src = SRC.get(code) if code else None
        f = ST.fields.get(body.get('cluster') or (src or {}).get('cluster')) or max(ST.fields.values(), key=lambda x: x.get('cmax', 0), default=None)
        if not f: raise HTTPException(409, 'no field yet')
        if src:                                                         # 지정 배출원에서 바람이 불어 가는 쪽 250 m
            m = next((m for m in f.get('members', []) if m['code'] == code), None)
            lon, lat = src['lon'], src['lat']; th = math.radians((m['wd'] if m else f.get('wd', 0)) + 180)
        else:
            lon, lat = f['cmax_lonlat']; th = math.radians(f.get('wd', 0) + 180)          # 최대 농도 지점에서 바람이 불어 가는 쪽 250 m
        lat2 = lat + 250 * math.cos(th) / 111320; lon2 = lon + 250 * math.sin(th) / (111320 * math.cos(math.radians(lat)))
        return complaint(dict(lat=lat2, lon=lon2, place=body.get('place', '인근 주민'), body=body.get('body', '돼지 분뇨 냄새가 심합니다'), intensity=4))
    PROD.produce(C.TOPIC['control'], json.dumps(body, ensure_ascii=False)); PROD.flush(2)
    emit('scenario', body); return dict(ok=True, sent=body)


@app.get('/api/config')
def get_config(): return httpx.get(f'{C.ENGINE_URL}/config', timeout=5).json()


@app.post('/api/config')
def set_config(body: dict = Body(...)):
    r = httpx.get(f'{C.ENGINE_URL}/config', params=dict(sigma=float(body.get('sigma', C.SIGMA_THETA))), timeout=5).json()
    emit('scenario', dict(cmd='sigma', value=r['sigma'])); return r


@app.get('/api/farm3d')
def farm3d(code: str):
    try: r = httpx.get(f'{C.ENGINE_URL}/farm3d', params=dict(code=code), timeout=30)
    except httpx.HTTPError: raise HTTPException(503, '확산 엔진에 연결하는 중입니다. 잠시 후 자동으로 다시 시도합니다.')
    if r.status_code != 200:
        try: msg = r.json().get('detail') or r.text[:200]
        except Exception: msg = r.text[:200] or '확산 엔진 오류'
        raise HTTPException(r.status_code if r.status_code < 500 else 503, msg)
    from fastapi import Response
    return Response(content=r.content, media_type='application/json')     # 큰 JSON 을 다시 변환하지 않고 그대로 전달


@app.get('/api/whatif')
def whatif(cluster: str, wd: float, ws: float, qscale: float = 1.0):
    try: r = httpx.get(f'{C.ENGINE_URL}/whatif', params=dict(cluster=cluster, wd=wd, ws=ws, qscale=qscale), timeout=60)
    except httpx.HTTPError: raise HTTPException(503, '가상 실험 계산이 늦어지고 있습니다. 잠시 후 다시 눌러 주세요.')
    if r.status_code != 200: raise HTTPException(503, '확산 엔진 오류')
    return r.json()


# ---------------- 레이크하우스 통계 (Trino) ----------------
_stat_cache = {}
STATS = {
    'monthly': 'SELECT * FROM lake.gold.monthly_trend ORDER BY month',
    'farm_rank': 'SELECT * FROM lake.gold.farm_rank ORDER BY rank LIMIT 20',
    'hourly_profile': 'SELECT * FROM lake.gold.hourly_profile ORDER BY hour',
    'cluster_exposure': 'SELECT * FROM lake.gold.cluster_exposure ORDER BY exposure_hours DESC, cmax_ppm DESC, cluster',
    'alarm_summary': 'SELECT * FROM lake.gold.alarm_summary ORDER BY n DESC',
    'complaint_summary': 'SELECT * FROM lake.gold.complaint_summary ORDER BY n DESC',
    'daily_stream': 'SELECT * FROM lake.gold.daily_stream ORDER BY day',
    'tables': 'SELECT * FROM lake.gold.lake_tables ORDER BY layer, table_name',
}


def trino_query(sql):
    con = trino.dbapi.connect(host=C.TRINO_HOST, port=C.TRINO_PORT, user='odortwin', catalog='lake')
    cur = con.cursor(); cur.execute(sql); cols = [c[0] for c in cur.description]
    return [dict(zip(cols, [v.isoformat() if hasattr(v, 'isoformat') else (float(v) if hasattr(v, 'as_integer_ratio') and not isinstance(v, (int, float)) else v) for v in r])) for r in cur.fetchall()]


@app.get('/api/stats/{name}')
def stats(name: str):
    if name not in STATS: raise HTTPException(404)
    hit = _stat_cache.get(name)
    if hit and time.time() - hit[0] < 30: return hit[1]
    try: rows = trino_query(STATS[name])
    except Exception as e: return dict(error=str(e)[:300])
    _stat_cache[name] = (time.time(), rows); return rows


@app.get('/api/lake/status')
def lake_status():
    runs = [jsonable(r) for r in q('SELECT * FROM batch_runs ORDER BY id DESC LIMIT 20') or []]
    try: snaps = trino_query('SELECT committed_at, snapshot_id, operation FROM lake.gold."daily_stream$snapshots" ORDER BY committed_at DESC LIMIT 10')
    except Exception: snaps = []
    try: n_snap = trino_query('SELECT count(*) AS n FROM lake.gold."daily_stream$snapshots"')[0]['n']
    except Exception: n_snap = None
    return dict(runs=runs, snapshots=snaps, snapshot_count=n_snap, airflow=airflow_status())


AIRFLOW_DSN = os.getenv('AIRFLOW_DB_DSN', C.PG_DSN.rsplit('/', 1)[0] + '/airflow')


def airflow_status():
    """Airflow DAG 상태 요약 (메타데이터 DB 읽기 전용). Airflow 가 없으면 None → 화면은 예비 배치로 표시"""
    try:
        with psycopg.connect(AIRFLOW_DSN, connect_timeout=2, row_factory=dict_row) as con:
            dags = con.execute("""SELECT d.dag_id, d.is_paused, d.next_dagrun_create_after AS next_at, r.state, r.start_date, r.end_date, r.run_id
                FROM dag d LEFT JOIN LATERAL (SELECT state, start_date, end_date, run_id FROM dag_run x WHERE x.dag_id = d.dag_id
                                              ORDER BY x.start_date DESC NULLS LAST LIMIT 1) r ON true
                WHERE d.dag_id LIKE 'odortwin%%' ORDER BY d.dag_id""").fetchall()
            lake = next((d for d in dags if d['dag_id'] == 'odortwin_lakehouse'), None)
            tasks = con.execute("SELECT state, count(*) AS n FROM task_instance WHERE dag_id='odortwin_lakehouse' AND run_id=%s GROUP BY state",
                                (lake['run_id'],)).fetchall() if lake and lake['run_id'] else []
            day = con.execute("""SELECT count(*) FILTER (WHERE state='success') AS ok, count(*) AS n FROM dag_run
                WHERE dag_id='odortwin_lakehouse' AND start_date > now() - interval '24 hours'""").fetchone()
            alive = con.execute("SELECT coalesce(max(latest_heartbeat) > now() - interval '60 seconds', false) AS a FROM job WHERE job_type = 'SchedulerJob'").fetchone()['a']
            design = {r['dag_id'] for r in con.execute("SELECT dag_id FROM dag_tag WHERE name = '설계'").fetchall()}
        return dict(dags=[jsonable({**{k: v for k, v in d.items() if k != 'run_id'}, 'design': d['dag_id'] in design}) for d in dags],
                    tasks={t['state'] or 'none': t['n'] for t in tasks}, day=day, alive=bool(alive))
    except Exception:
        return None


WEB = os.path.join(C.DEMO, 'web', 'dist')
if os.path.isdir(os.path.join(WEB, 'assets')): app.mount('/assets', StaticFiles(directory=os.path.join(WEB, 'assets')), name='assets')
BASEMAP = os.path.join(C.DATA, 'basemap')                         # 오프라인 배경 지도 (제주 PMTiles·스프라이트·글리프)
if os.path.isdir(BASEMAP): app.mount('/basemap', StaticFiles(directory=BASEMAP), name='basemap')


@app.get('/{path:path}')
def spa(path: str):
    """화면 주소(/ops, /insight 등)는 index.html 로 돌려준다 (React 라우터가 처리)"""
    from fastapi.responses import FileResponse
    from pathlib import Path
    root = Path(WEB).resolve(); f = (root / path).resolve()
    if path and f.is_relative_to(root) and f.is_file(): return FileResponse(f)      # web/dist 밖의 파일은 절대 내주지 않는다
    return FileResponse(root / 'index.html')

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    C.serve(app, 8000)
