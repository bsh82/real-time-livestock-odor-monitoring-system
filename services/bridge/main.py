"""MQTT ↔ Kafka 브리지.
상향: jeju/odor/+/telemetry → odor.raw (key = 센서 코드, 수신 시각 ingest_ms 추가, 형식 오류는 odor.dlq)
      jeju/odor/+/status → device.status,  jeju/odor/+/cmd/ack → cmd.ack
하향: cmd.down → jeju/odor/{code}/cmd
매초 처리량을 ops.metrics 로 보낸다.
실행: python -m services.bridge.main
"""
import json, sys, threading, time
import paho.mqtt.client as mqtt
from confluent_kafka import Consumer, Producer
from services.common import config as C

REQ = {'code', 'ts_ms', 'nh3'}
prod = Producer({'bootstrap.servers': C.KAFKA, 'linger.ms': 10, 'compression.type': 'lz4'})
stat = dict(mqtt_in=0, kafka_out=0, dlq=0, cmd_down=0)


def on_message(cli, ud, msg):
    stat['mqtt_in'] += 1
    parts = msg.topic.split('/'); code = parts[2] if len(parts) > 2 else ''
    now = int(time.time() * 1000)
    try:
        d = json.loads(msg.payload)
    except Exception:
        prod.produce(C.TOPIC['dlq'], json.dumps(dict(topic=msg.topic, raw=msg.payload.decode('utf-8', 'replace'), at_ms=now))); stat['dlq'] += 1; return
    if msg.topic.endswith('/telemetry'):
        if not REQ <= d.keys() or d['code'] != code:
            prod.produce(C.TOPIC['dlq'], json.dumps(dict(topic=msg.topic, raw=d, reason='schema', at_ms=now))); stat['dlq'] += 1; return
        d['ingest_ms'] = now
        prod.produce(C.TOPIC['raw'], json.dumps(d), key=code)
    elif msg.topic.endswith('/status'):
        d.setdefault('at_ms', now); prod.produce(C.TOPIC['status'], json.dumps(d), key=code)
    elif msg.topic.endswith('/ack'):
        prod.produce(C.TOPIC['cmd_ack'], json.dumps(d, ensure_ascii=False), key=code)
    stat['kafka_out'] += 1
    prod.poll(0)


def downlink(cli):
    cons = Consumer({'bootstrap.servers': C.KAFKA, 'group.id': 'bridge-downlink', 'auto.offset.reset': 'latest'})
    cons.subscribe([C.TOPIC['cmd_down']])
    while True:
        m = cons.poll(0.5)
        if m is None or m.error(): continue
        try:
            d = json.loads(m.value()); cli.publish(C.MQTT_CMD.format(code=d['code']), json.dumps(d, ensure_ascii=False), qos=1); stat['cmd_down'] += 1
        except Exception as e:
            print('downlink error', e, flush=True)


def main():
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='odortwin-bridge')
    cli.on_message = on_message
    cli.on_connect = lambda c, u, f, rc, p=None: [c.subscribe(t, qos=1) for t in ('jeju/odor/+/telemetry', 'jeju/odor/+/status', 'jeju/odor/+/cmd/ack')]
    cli.connect(C.MQTT_HOST, C.MQTT_PORT, keepalive=30); cli.loop_start()
    threading.Thread(target=downlink, args=(cli,), daemon=True).start()
    last = dict(stat)
    while True:
        time.sleep(1.0)
        rate = {k: stat[k] - last[k] for k in stat}; last = dict(stat)
        prod.produce('ops.metrics', json.dumps(dict(component='bridge', at_ms=int(time.time() * 1000), **{f'{k}_per_s': v for k, v in rate.items()}, **stat)))
        prod.poll(0)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8'); main()
