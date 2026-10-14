"""농가 센서 게이트웨이 시뮬레이터.
124개소 실측 이력(30분 간격)을 데이터 시각 1분 간격으로 선형 보간해 재생하고, 게이트웨이마다 MQTT 클라이언트 1개로 송신한다.
- 상향: jeju/odor/{code}/telemetry (QoS 1), 상태: jeju/odor/{code}/status (retained, LWT = offline)
- 하향: jeju/odor/{code}/cmd 수신 → 'mitigate'(저감시설 가동)면 농도를 점차 낮추고 cmd/ack 로 응답
- 재생 시계: sim.clock 토픽으로 매초 알림. 제어: control.sim 토픽 (speed, pause, resume, spike, stuck, offline, clear)
재생 구간은 앞으로만 흐른다(이벤트 시각이 되돌아가면 Flink 윈도가 깨지므로 반복 재생하지 않음).
실행: python -m services.simulator.main
"""
import json, os, socket, sys, threading, time
from datetime import datetime
import numpy as np, pandas as pd
import paho.mqtt.client as mqtt
from confluent_kafka import Consumer, Producer
from services.common import config as C

VARS = ['nh3', 'h2s', 'temp', 'hum', 'ws', 'wd', 'pm', 'pres']
MAX_GAP_S = 45 * 60          # 가장 가까운 실측이 45분보다 멀면 미수신으로 둔다


class Clock:
    def __init__(self, start, speed):
        self.lock = threading.Lock(); self.base_data = start.timestamp(); self.base_wall = time.time(); self.speed = speed; self.paused = False
    def now(self):
        with self.lock:
            return self.base_data if self.paused else self.base_data + (time.time() - self.base_wall) * self.speed
    def set_speed(self, s):
        with self.lock: self.base_data = self.now_unlocked(); self.base_wall = time.time(); self.speed = float(s)
    def now_unlocked(self): return self.base_data if self.paused else self.base_data + (time.time() - self.base_wall) * self.speed
    def pause(self, p):
        with self.lock:
            if p == self.paused: return
            self.base_data = self.now_unlocked(); self.base_wall = time.time(); self.paused = p


class Gateway:
    def __init__(self, code, df):
        self.code = code; self.t = C.epoch_s(df.ts)
        self.v = {k: df[k].to_numpy(dtype='float64') for k in VARS}
        self.seq = 0; self.spike = None; self.stuck = None; self.mitigate = None; self.offline_until = 0.0; self.dead = False
        cid = f'gw-{code}'
        self.cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, clean_session=True)
        self.cli.will_set(C.MQTT_STATUS.format(code=code), json.dumps(dict(code=code, status='offline')), qos=1, retain=True)
        self.cli.on_connect = self._on_connect; self.cli.on_message = self._on_cmd
        self.cli.connect_async(C.MQTT_HOST, C.MQTT_PORT, keepalive=5); self.cli.loop_start()

    def _on_connect(self, cli, ud, flags, rc, props=None):
        cli.publish(C.MQTT_STATUS.format(code=self.code), json.dumps(dict(code=self.code, status='online', at_ms=int(time.time() * 1000))), qos=1, retain=True)
        cli.subscribe(C.MQTT_CMD.format(code=self.code), qos=1)

    def _on_cmd(self, cli, ud, msg):
        try: cmd = json.loads(msg.payload)
        except Exception: return
        ack = dict(code=self.code, cmd_id=cmd.get('cmd_id'), action=cmd.get('action'), status='accepted', at_ms=int(time.time() * 1000))
        if cmd.get('action') == 'mitigate':
            now = SIM.clock.now(); self.mitigate = (now, now + float(cmd.get('hours', 6)) * 3600)
            if self.spike: t0, t1, fac, fl = self.spike; self.spike = (t0, min(t1, now), fac, fl)     # 저감 조치 → 급증도 서서히 끝남
            ack['status'] = 'done'; ack['note'] = '저감시설 가동'
        cli.publish(C.MQTT_ACK.format(code=self.code), json.dumps(ack, ensure_ascii=False), qos=1)

    def value(self, ts):
        i = np.searchsorted(self.t, ts)
        near = min(abs(ts - self.t[min(i, len(self.t) - 1)]), abs(ts - self.t[max(i - 1, 0)]))
        if near > MAX_GAP_S: return None
        out = {k: float(np.interp(ts, self.t, v)) for k, v in self.v.items()}
        if self.stuck and ts < self.stuck[1]: out['nh3'] = self.stuck[0]
        f = 1.0
        if self.spike:                                                     # (시작, 끝, 배율, 하한 ppm): 5분 증가 → 유지 → 10분 감소
            t0, t1, fac, fl = self.spike
            if ts < t0 + 300: r = max(0.0, (ts - t0) / 300)
            elif ts < t1: r = 1.0
            else: r = max(0.0, 1 - (ts - t1) / 600)
            out['nh3'] = min(max(out['nh3'] * (1 + (fac - 1) * r), fl * r), 80.0)
            if ts > t1 + 600: self.spike = None
        if self.mitigate:
            t0, t1 = self.mitigate
            if ts < t1: f *= 1 - 0.65 * min(1.0, (ts - t0) / 900)
            else: self.mitigate = None
        out['nh3'] *= f
        return out

    def publish(self, ts):
        if self.dead: return False
        val = self.value(ts)
        if val is None: return False
        self.seq += 1
        msg = dict(code=self.code, ts_ms=int(ts * 1000), seq=self.seq, sent_ms=int(time.time() * 1000), **{k: round(v, 3) for k, v in val.items()})
        self.cli.publish(C.MQTT_TELEMETRY.format(code=self.code), json.dumps(msg), qos=1)
        return True

    def kill(self, seconds):
        """통신 두절: DISCONNECT 없이 소켓을 끊어 브로커가 LWT 를 내보내게 한다."""
        self.dead = True; self.offline_until = time.time() + seconds
        self.cli.loop_stop()
        s = self.cli.socket()
        if s is not None:
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            s.close()

    def revive(self):
        self.dead = False
        try: self.cli.reconnect()
        except Exception: pass
        self.cli.loop_start()


class Simulator:
    def __init__(self):
        S = pd.read_csv(os.path.join(C.DATA, 'sources.csv'), encoding='utf-8-sig', dtype={'code': str})
        lo, hi = pd.Timestamp(C.REPLAY_START) - pd.Timedelta('2h'), pd.Timestamp(C.REPLAY_END) + pd.Timedelta('2h')
        D = pd.read_parquet(os.path.join(C.DATA, 'sensors.parquet'))
        D['ts'] = D.ts.dt.tz_localize(C.KST)
        D = D[(D.ts >= lo) & (D.ts <= hi)].sort_values('ts')
        self.clock = Clock(C.REPLAY_START, C.REPLAY_SPEED)
        self.gw = {}
        for code in S.code:
            d = D[D.code == code].dropna(subset=['nh3'])
            if len(d) >= 2: self.gw[code] = Gateway(code, d.fillna(0))
        print(f'gateways {len(self.gw)} / {len(S)} (나머지는 재생 구간에 실측 없음)', flush=True)
        self.prod = Producer({'bootstrap.servers': C.KAFKA, 'linger.ms': 20})
        self.last_tick = None; self.sent = 0

    def control(self, c):
        cmd = c.get('cmd'); code = c.get('code'); g = self.gw.get(code)
        now = self.clock.now()
        if cmd == 'speed': self.clock.set_speed(c['value'])
        elif cmd == 'pause': self.clock.pause(True)
        elif cmd == 'resume': self.clock.pause(False)
        elif cmd == 'spike' and g:                                    # 새 급증은 이전 저감·고착 상태와 무관하게 시작
            g.mitigate = g.stuck = None; g.spike = (now, now + float(c.get('minutes', 60)) * 60, float(c.get('factor', 8)), float(c.get('floor', 0)))
        elif cmd == 'stuck' and g: g.stuck = (g.value(now)['nh3'] if g.value(now) else 1.0, now + float(c.get('minutes', 180)) * 60)
        elif cmd == 'offline' and g: g.kill(float(c.get('seconds', 60)))
        elif cmd == 'clear':
            for x in self.gw.values(): x.spike = x.stuck = x.mitigate = None
        print('control', c, flush=True)

    def control_loop(self):
        cons = Consumer({'bootstrap.servers': C.KAFKA, 'group.id': 'simulator', 'auto.offset.reset': 'latest'})
        cons.subscribe([C.TOPIC['control']])
        while True:
            m = cons.poll(0.5)
            if m is None or m.error(): continue
            try: self.control(json.loads(m.value()))
            except Exception as e: print('control error', e, flush=True)

    def run(self):
        threading.Thread(target=self.control_loop, daemon=True).start()
        time.sleep(2.0)
        step = C.TICK_DATA_SEC; last_clock = 0
        while True:
            now = self.clock.now()
            if self.last_tick is None: self.last_tick = (now // step) * step - step
            while self.last_tick + step <= now:                         # 지난 데이터-분마다 한 번씩 송신
                self.last_tick += step
                for g in self.gw.values(): self.sent += g.publish(self.last_tick)
            for g in self.gw.values():
                if g.dead and time.time() >= g.offline_until: g.revive()
            if time.time() - last_clock >= 1.0:
                last_clock = time.time()
                self.prod.produce(C.TOPIC['clock'], json.dumps(dict(replay_ms=int(now * 1000), speed=self.clock.speed, paused=self.clock.paused,
                                                                    wall_ms=int(last_clock * 1000), sent=self.sent, gateways=len(self.gw))))
                self.prod.poll(0)
            time.sleep(0.05)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    SIM = Simulator(); SIM.run()
