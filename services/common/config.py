"""OdorTwin 공통 설정. 환경 변수로 덮어쓸 수 있다."""
import os
import json
from datetime import datetime, timezone, timedelta

DEMO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OPK = os.path.dirname(DEMO)
DATA = os.path.join(DEMO, 'data')
RUNTIME = os.getenv('ODORTWIN_RUNTIME', os.path.join(DEMO, 'runtime')); os.makedirs(RUNTIME, exist_ok=True)
KST = timezone(timedelta(hours=9))

MQTT_HOST = os.getenv('MQTT_HOST', '127.0.0.1'); MQTT_PORT = int(os.getenv('MQTT_PORT', 1883))
KAFKA = os.getenv('KAFKA', '127.0.0.1:9092')
PG_DSN = os.getenv('PG_DSN', 'postgresql://odor:odortwin@127.0.0.1:5433/odor')
TRINO_HOST = os.getenv('TRINO_HOST', '127.0.0.1'); TRINO_PORT = int(os.getenv('TRINO_PORT', 8088))
ENGINE_URL = os.getenv('ENGINE_URL', 'http://127.0.0.1:8010')
# 서비스 사이 주소는 모두 127.0.0.1: Windows 에서 'localhost' 는 IPv6(::1)를 먼저 시도하는데, 컨테이너 포트는 127.0.0.1 에만 열려 있어
# 연결마다 약 2초씩 실패한 뒤 IPv4 로 넘어간다 (2026-10-14 Kafka 소비자가 이 때문에 멈춰 화면 실시간 연결이 끊김).

# 재생 기본값: 2024-11-10 00:00 부터 앞으로만 진행 (센서 107곳 활성, 11-10~13 배출 급증 많음).
# 60배속이면 하루가 24분, 구간 끝(12-01)까지 약 8.4시간 → 전시 하루 동안 되감기 없이 돈다.
REPLAY_START = datetime.fromisoformat(os.getenv('REPLAY_START', '2024-11-10T00:00:00+09:00'))
REPLAY_END = datetime.fromisoformat(os.getenv('REPLAY_END', '2024-12-01T00:00:00+09:00'))
REPLAY_SPEED = float(os.getenv('REPLAY_SPEED', 60))
TICK_DATA_SEC = 60            # 재생 데이터 간격(데이터 시각 1분) — 30분 실측 사이는 선형 보간

TOPIC = dict(raw='odor.raw', clean='odor.clean', agg='odor.agg10m', emission='odor.emission', alert='odor.alert',
             status='device.status', cmd_down='cmd.down', cmd_ack='cmd.ack', clock='sim.clock', control='control.sim',
             wind='weather.wind', field='odor.field', dlq='odor.dlq', complaint='odor.complaint')
MQTT_TELEMETRY = 'jeju/odor/{code}/telemetry'
MQTT_STATUS = 'jeju/odor/{code}/status'
MQTT_CMD = 'jeju/odor/{code}/cmd'
MQTT_ACK = 'jeju/odor/{code}/cmd/ack'

# 배출량 가정값 (2026-10-13 합의: 엄밀성보다 시연, 화면에 "가정값" 표시)
VENT_BASE = {'barn': 30.0, 'storage': 10.0, 'proxy': 20.0}     # m³/s
BARN = {'비육돈사', '육성돈사', '분만돈사', '임신돈사', '산란계사', '육계사'}
STORAGE = {'액비저장조', '분뇨처리장', '퇴비사'}
PPM_ALERT = dict(caution=0.6, warning=1.0)                     # 수용체 예측 농도 경보 [ppm]
# 시연 설정 (data/demo_config.json, 예시: config/demo_config.example.json). 실제 센서 코드는 코드에 두지 않는다.
#   suspect : 데이터 이상 센서 {코드: 사유} → 확산 계산에서 빼고 화면에서 '점검'으로 표시
#   scenario: 발표용 시나리오 대상 (배출 급증·통신 두절 센서, 이야기 권역)
def _demo_cfg():
    try: return json.load(open(os.path.join(DATA, 'demo_config.json'), encoding='utf-8'))
    except FileNotFoundError: return {}
DEMO_CFG = _demo_cfg()
SUSPECT = DEMO_CFG.get('suspect', {})
SCENARIO = {'story_cluster': 'C01', 'west_cluster': 'C02', **DEMO_CFG.get('scenario', {})}
BIND_HOST = os.getenv('BIND_HOST', '127.0.0.1')                  # API·엔진은 이 PC 에서만 접속 (전시장 네트워크에 열지 않음)


def serve(app, port):
    """uvicorn 실행. 기본(루프백)이면 127.0.0.1 과 ::1 을 둘 다 열어, 브라우저가 localhost 로 접속해도 IPv6 시도에서 막히지 않게 한다."""
    import socket, uvicorn
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
    if BIND_HOST not in ('127.0.0.1', 'localhost'):
        server.config.host, server.config.port = BIND_HOST, port; server.run(); return
    socks = []
    for fam, addr in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
            if fam == socket.AF_INET6: s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((addr, port)); s.listen(2048); s.set_inheritable(True); socks.append(s)
        except OSError as e: print(f'bind {addr}:{port} 실패: {e}', flush=True)
    server.run(sockets=socks)
U_MIN = 0.5                                                     # 풍속 하한 [m/s] (무풍 처리)
# 풍향 흔들림(meandering) 후처리: θ 주변 가우시안 σ_θ 로 농도장 평균 (D등급 σ_y 에 맞춘 값 ≈ 3°, 0 이면 끔)
SIGMA_THETA = float(os.getenv('SIGMA_THETA', 3.0))


def epoch_s(ts):
    """tz-aware datetime Series → epoch 초 (pandas 3 의 us/s 해상도와 무관하게)"""
    import pandas as pd
    return (ts.dt.tz_convert('UTC') - pd.Timestamp('1970-01-01', tz='UTC')).dt.total_seconds().to_numpy()
