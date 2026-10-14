"""운영 DB 초기 적재: sources(메타·경보 기준·환기량 가정값) + sensor_history(2023-02 ~ 2025-04 전체 이력, 레이크하우스 초기 적재용).
실행: python -m services.common.load_meta [--history]
"""
import io, os, sys
import numpy as np, pandas as pd, psycopg
from services.common import config as C


def vent_base(r):
    if r.role == 'proxy': return C.VENT_BASE['proxy']
    if r.role != 'source': return None
    return C.VENT_BASE['barn'] if r.place in C.BARN else C.VENT_BASE['storage']


def main(history):
    S = pd.read_csv(os.path.join(C.DATA, 'sources.csv'), encoding='utf-8-sig', dtype={'code': str})
    D = pd.read_parquet(os.path.join(C.DATA, 'sensors.parquet'))
    p95 = D[D.nh3 < 100].groupby('code').nh3.quantile(0.95)
    S['nh3_p95'] = S.code.map(p95).astype(float); S['vent_base'] = [vent_base(r) for r in S.itertuples()]
    cols = ['code', 'farm', 'loc', 'place', 'pos', 'role', 'research', 'livestock', 'cluster', 'lat', 'lon', 'x', 'y', 'vent_base', 'nh3_p95']
    with psycopg.connect(C.PG_DSN, autocommit=True) as con:
        con.execute('TRUNCATE sources')
        with con.cursor().copy(f'COPY sources ({",".join(cols)}) FROM STDIN') as cp:
            for row in S[cols].itertuples(index=False):
                cp.write_row([None if (isinstance(v, float) and np.isnan(v)) else v for v in row])
        print('sources', len(S))
        if history:
            n = con.execute('SELECT count(*) FROM sensor_history').fetchone()[0]
            if n: print('sensor_history already loaded', n); return
            D = D.sort_values(['code', 'ts'])
            D['ts'] = D.ts.dt.tz_localize(C.KST)
            hc = ['code', 'ts', 'nh3', 'h2s', 'temp', 'hum', 'ws', 'wd', 'pm', 'pres']
            buf = io.StringIO(); D[hc].to_csv(buf, index=False, header=False, na_rep='\\N'); buf.seek(0)
            with con.cursor().copy(f'COPY sensor_history ({",".join(hc)}) FROM STDIN WITH (FORMAT csv, NULL \'\\N\')') as cp:
                while chunk := buf.read(1 << 20): cp.write(chunk)
            print('sensor_history', con.execute('SELECT count(*) FROM sensor_history').fetchone()[0])


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8'); main('--history' in sys.argv)
