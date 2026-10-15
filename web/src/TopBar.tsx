import type { ReactNode } from 'react'
import dayjs from 'dayjs'
import { useStore } from './store'

/** 상단 막대: 이름 · 재생 시각 · (관제 화면만) 핵심 지표 3개 · 시스템 상태 한 줄 */
export default function TopBar({ title, kpis = true, extra }: { title: string; kpis?: boolean; extra?: ReactNode }) {
  const { kpi, connected } = useStore()
  const clk = kpi?.clock?.replay_ms ? dayjs(kpi.clock.replay_ms) : null
  const eng = kpi?.engine || {}
  const gpu = !!eng.device
  const upd = gpu && eng.infer_ms != null ? Math.round((eng.infer_ms || 0) + (eng.render_ms || 0)) : null
  const problem = !connected ? '연결 끊김' : kpi?.kafka_ok === false ? 'Kafka 정지' : !gpu ? 'GPU 정지' : null
  return (
    <div className="topbar">
      <div className="brand"><b>OdorTwin</b><span>{title}</span></div>
      <div className="clock">{clk ? clk.format('YYYY-MM-DD HH:mm') : '--:--'}</div>
      {kpi?.clock?.paused ? <span className="badge paused">일시정지</span> : <span className="badge live">실측 재생 ×{kpi?.clock?.speed ?? '-'}</span>}
      {extra}
      <div className="kpis">
        {kpis && <>
          <K l="센서 수신" v={`${kpi?.sensors_live ?? '-'}/${kpi?.sensors_total ?? '-'}`} />
          <K l="전 구간 지연" v={kpi?.latency_ms != null ? `${kpi.latency_ms} ms` : '-'} tip="센서 송신 → MQTT → Kafka → Flink → 화면 (중앙값)" />
          <K l="확산 갱신" v={gpu ? `${upd ?? '-'} ms` : '정지'} bad={!gpu} tip="권역 확산 지도 1회 갱신 (GPU 추론 + 지도 생성, 같은 풍향은 재사용)" />
        </>}
        <span className={`sys ${problem ? 'bad' : 'ok'}`}><span className="dot" style={{ background: problem ? '#ef4444' : '#22c55e', marginRight: 0 }} />{problem ?? '시스템 정상'}</span>
      </div>
    </div>
  )
}
function K({ l, v, bad = false, tip }: { l: string; v: any; bad?: boolean; tip?: string }) {
  return <div className={`kpi ${bad ? 'bad' : ''}`} title={tip}><span className="l">{l}</span><span className="v">{v}</span></div>
}
