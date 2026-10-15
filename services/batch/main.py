"""레이크하우스 배치 — 예비 실행기 (평소에는 Airflow DAG 가 돌린다: orchestration/airflow_dags/odortwin_lakehouse.py).
Airflow 를 쓸 수 없을 때만 이 서비스로 같은 단계(services/batch/steps.py)를 일정 간격으로 차례대로 돌린다.
실행: python -m services.batch.main [--once]
"""
import os, sys, time, traceback
from services.batch import steps as S

INTERVAL = int(os.getenv('BATCH_INTERVAL', 120))


def cycle():
    S.bronze_incremental(); t0 = time.time(); total = 0
    for name in list(S.SILVER) + list(S.GOLD_STREAM) + list(S.GOLD_HISTORY):
        try: total += S.refresh(name)
        except Exception as e: print('step error', name, str(e)[:200], flush=True)
    S.lake_tables()
    S.log('silver·gold 재생성 (예비 배치)', total, 'ok', f'{len(S.ALL)}개 테이블', t0)
    print(f'batch ok: silver/gold {total} rows, {time.time() - t0:.1f}s', flush=True)


def main(once):
    S.ensure_layers()
    while True:
        try: cycle()
        except Exception as e:
            traceback.print_exc(); S.log('batch', 0, 'error', str(e))
        if once: break
        time.sleep(INTERVAL)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8'); main('--once' in sys.argv)
