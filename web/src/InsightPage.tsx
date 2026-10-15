import { useEffect, useMemo, useState } from 'react'
import dayjs from 'dayjs'
import ReactECharts from 'echarts-for-react'
import TopBar from './TopBar'
import MapView from './MapView'
import { useStore, post, clusterCenter, STATE_KO } from './store'
import { dir16 } from './util'
import { fmtPpmU } from './colormap'

function useStat(name: string, every = 30000) {
  const [d, setD] = useState<any[]>([])
  useEffect(() => { const load = () => fetch(`/api/stats/${name}`).then((r) => r.json()).then((x) => Array.isArray(x) && setD(x)).catch(() => {}); load(); const t = setInterval(load, every); return () => clearInterval(t) }, [name])
  return d
}

let seeded = false                      // 예시 민원은 화면을 열 때 한 번만

/** 모니터 2 — 시연 진행 · 민원 역추적 · 가상 실험 · 데이터 흐름/레이크하우스 */
export default function InsightPage() {
  const s = useStore()
  const STORY_CLUSTER = s.demo.story_cluster
  const [selId, setSelId] = useState<number | null>(null)
  const [pick, setPick] = useState(false)
  const c0 = clusterCenter(STORY_CLUSTER)
  const [view, setView] = useState<any>(c0 ? { longitude: c0[0], latitude: c0[1], zoom: 11.4 } : { longitude: 126.3, latitude: 33.38, zoom: 11.2 })
  useEffect(() => { const c = clusterCenter(STORY_CLUSTER); if (c && !s.complaints.length) setView({ longitude: c[0], latitude: c[1], zoom: 11.4 }) }, [Object.keys(s.clusters).length])
  // 새 민원 → 선택하고 그 위치로
  const newest = s.complaints[0]
  useEffect(() => { if (newest) { setSelId(newest.id); setView({ longitude: newest.lon, latitude: newest.lat, zoom: 13 }) } }, [newest?.id])
  // 가상 실험 결과 → 그 권역으로
  useEffect(() => { if (s.whatif) { const c = clusterCenter(s.whatif.cluster); if (c) setView({ longitude: c[0], latitude: c[1], zoom: 12.4 }) } }, [s.whatif?.png])
  // 민원이 하나도 없으면 예시 민원 1건 (start_all -Reset 이 민원 표를 비우므로)
  useEffect(() => {
    if (!seeded && s.connected && s.complaints.length === 0 && s.fields[STORY_CLUSTER]) { seeded = true; post('/api/scenario', { cmd: 'complaint', cluster: STORY_CLUSTER, place: '예시 민원' }).catch(() => {}) }
  }, [s.connected, s.complaints.length, !!s.fields[STORY_CLUSTER], STORY_CLUSTER])

  async function addComplaint(ll: number[]) {
    setPick(false)
    await post('/api/complaints', { lat: ll[1], lon: ll[0], place: '지도 접수', body: '악취 민원 (관제 화면 접수)', intensity: 3 })
  }
  const w = s.whatif
  return (
    <div className="app">
      <TopBar title="통계 · 민원 · 시스템" kpis={false} />
      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr 1fr', gridTemplateRows: '540px 1fr', gap: 8, padding: 8, minHeight: 0 }}>
        <div style={{ gridRow: '1 / 3', minHeight: 0, display: 'grid' }}><StoryRail pick={pick} setPick={setPick} /></div>
        <div className="panel">
          <h3>지도
            {w ? <><span className="pill caution">가상 실험 · {w.cluster} {dir16(w.wd)} {Number(w.ws).toFixed(1)} m/s</span>
              <button style={{ marginLeft: 'auto' }} onClick={() => s.set({ whatif: null })}>실시간으로 복귀</button></>
              : <span className="pill ok">실시간</span>}
            {pick && <span className="r" style={{ color: '#fde68a' }}>지도를 눌러 민원 위치를 지정하세요</span>}</h3>
          <div style={{ position: 'relative', flex: 1 }}>
            <MapView view={view} showParticles={false} showAttribution whatif={w} onPick={pick ? addComplaint : undefined} reserve={(_w, h) => [[0, h - 48, 380, h]]} />
            <div className="overlay" style={{ left: 10, bottom: 10, padding: '5px 10px', fontSize: 12.5, color: 'var(--text-2)' }}>
              <span className="dot" style={{ background: '#d946ef' }} />민원 <span style={{ color: '#e879f9' }}>━</span> 기여 농가 (굵을수록 큼)</div>
          </div>
          <div className="muted" style={{ fontSize: 11.5, padding: '5px 12px', borderTop: '1px solid var(--line)' }}>민원 기여도·가상 실험 = 확산 모델 예측 (U-FNO+F → FNO · 배출량 가정값 · 중립·정상 상태) · 지도 © OpenStreetMap contributors · Protomaps</div>
        </div>
        <ComplaintPanel selId={selId} onSel={(id) => { setSelId(id); const c = s.complaints.find((x) => x.id === id); if (c) setView({ longitude: c.lon, latitude: c.lat, zoom: 13 }) }} />
        <WhatIf />
        <DataFlow />
      </div>
    </div>
  )
}

/** 왼쪽: 발표자용 시연 순서 */
function StoryRail({ pick, setPick }: { pick: boolean; setPick: (v: boolean) => void }) {
  const { kpi, sensors, alarms, set, demo, sources } = useStore()
  const SPIKE_CODE = demo.spike_code ?? ''; const spikeFarm = sources[SPIKE_CODE]?.farm ?? '지정 농가'
  const OFF_CODE = demo.offline_code ?? ''; const offFarm = sources[OFF_CODE]?.farm ?? '지정 게이트웨이'
  const sc = (body: any) => post('/api/scenario', body)
  const [sigma, setSigma] = useState<number | null>(null)
  useEffect(() => { fetch('/api/config').then((r) => r.json()).then((c) => setSigma(c.sigma)).catch(() => {}) }, [])
  const setSigmaApi = async (v: number) => { const r = await post('/api/config', { sigma: v }); setSigma(r.sigma) }
  const sv = sensors[SPIKE_CODE]
  const al = Object.values(alarms).filter((a) => a.code === SPIKE_CODE && a.kind === 'EMISSION_HIGH').sort((a, b) => b.id - a.id)[0]
  const speed = kpi?.clock?.speed; const paused = kpi?.clock?.paused
  return (
    <div className="panel">
      <h3>시연 진행 <span className="r">{paused ? '일시정지' : `×${speed ?? '-'} 재생`}</span></h3>
      <div className="scroll">
        <div className="step"><div className="h"><span className="no">1</span>정상 운영</div>
          <div className="desc">실측 124곳 재생 · 20개 권역 확산 계산</div>
          <div className="btns">{[60, 600].map((v) => <button key={v} className={speed === v ? 'primary' : ''} onClick={() => sc({ cmd: 'speed', value: v })}>×{v}</button>)}
            <button onClick={() => sc({ cmd: paused ? 'resume' : 'pause' })}>{paused ? '재개' : '일시정지'}</button></div></div>
        <div className="step"><div className="h"><span className="no">2</span>배출 급증</div>
          <div className="desc">{spikeFarm} {sources[SPIKE_CODE]?.place ?? ''} NH₃ 급증 → 5분 연속 기준 초과 시 경보</div>
          <div className="btns"><button className="primary" disabled={!SPIKE_CODE} onClick={() => sc({ cmd: 'spike', code: SPIKE_CODE, factor: 10, floor: 30, minutes: 600 })}>배출 급증 실행</button></div>
          <div className="live">{spikeFarm} {sv ? sv.nh3.toFixed(1) : '-'} / 기준 {sv?.thr ? sv.thr.toFixed(1) : '-'} ppm</div></div>
        <div className="step"><div className="h"><span className="no">3</span>경보 확인 · 3D</div>
          <div className="desc">모니터 1: 경보 카드 [확인] → [3D 보기]</div>
          <div className="live">{spikeFarm} 경보: {al ? STATE_KO[al.state] : '없음'}</div></div>
        <div className="step"><div className="h"><span className="no">4</span>저감 조치 → 해제</div>
          <div className="desc">모니터 1: [저감 조치] → 게이트웨이 명령(MQTT) → 농도 감소 → [해제]</div></div>
        <div className="step"><div className="h"><span className="no">5</span>민원 역추적</div>
          <div className="desc">민원 지점의 예측 농도를 농가별로 분해</div>
          <div className="btns"><button className="primary" onClick={() => sc(SPIKE_CODE ? { cmd: 'complaint', code: SPIKE_CODE, place: `${spikeFarm} 인근 주민` } : { cmd: 'complaint', cluster: demo.story_cluster, place: '인근 주민' })}>민원 발생</button></div></div>
        <div className="step"><div className="h"><span className="no">6</span>데이터 흐름 · 레이크하우스</div>
          <div className="desc">오른쪽 아래: 실시간 스트림 + 배치(Iceberg)</div></div>
        <div className="step"><div className="h"><span className="no">7</span>가상 실험</div>
          <div className="desc">가운데 아래: GPU 수 초 vs CFD 35~45분</div></div>
        <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <button onClick={() => { sc({ cmd: 'clear' }); set({ whatif: null }) }}>시나리오 해제</button>
            <button disabled={!OFF_CODE} onClick={() => sc({ cmd: 'offline', code: OFF_CODE, seconds: 60 })}>통신 두절 · {offFarm}</button>
          </div>
          <details><summary>추가 시나리오 ▸</summary>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 }}>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              </div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}><span className="muted" style={{ fontSize: 12.5 }}>재생</span>
                {[30, 180].map((v) => <button key={v} className={speed === v ? 'primary' : ''} onClick={() => sc({ cmd: 'speed', value: v })}>×{v}</button>)}</div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}><span className="muted" style={{ fontSize: 12.5 }}>풍향 변동 σθ</span>
                {[0, 3, 6].map((v) => <button key={v} className={sigma === v ? 'primary' : ''} onClick={() => setSigmaApi(v)}>{v === 0 ? '끔' : `${v}°`}</button>)}</div>
              <div className="muted" style={{ fontSize: 12 }}>θ ± 3σθ 농도장 평균 → 정상 상태 해석의 좁은 측면 폭 보완</div>
              <button className={pick ? 'primary' : ''} onClick={() => setPick(!pick)}>지도에서 민원 위치 지정</button>
            </div>
          </details>
        </div>
      </div>
    </div>
  )
}

/** 민원 역추적: 냄새는 어디서 왔나? */
function ComplaintPanel({ selId, onSel }: { selId: number | null; onSel: (id: number) => void }) {
  const { complaints, demo, sources } = useStore()
  const sp = demo.spike_code; const spBody = sp ? { cmd: 'complaint', code: sp, place: `${sources[sp]?.farm ?? '지정 농가'} 인근 주민` } : { cmd: 'complaint', cluster: demo.story_cluster, place: '인근 주민' }
  const sel = complaints.find((c) => c.id === selId) || complaints[0]
  const att = byFarm(sel?.attribution?.sources).slice(0, 5)
  return (
    <div className="panel">
      <h3>민원 역추적 — 냄새는 어디서 왔나?<span className="r">{complaints.length}건</span></h3>
      {!sel ? (
        <div style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 14, fontSize: 14 }}>
          <div>① 민원 위치 → ② 그 지점의 예측 농도를 농가별로 분해 → ③ 기여 농가 순위</div>
          <div><button className="primary big" onClick={() => post('/api/scenario', spBody)}>민원 발생 시연</button></div>
        </div>
      ) : (
        <div style={{ padding: '10px 16px', display: 'flex', flexDirection: 'column', gap: 6, minHeight: 0, flex: 1 }}>
          <div style={{ fontSize: 14 }}><b>{sel.place || '민원'}</b> <span className="muted">· {dayjs(sel.ts).format('MM-DD HH:mm')} · 강도 {sel.intensity} · 예측 농도 합계</span> <span className="mono">{sel.attribution?.total != null ? fmtPpmU(sel.attribution.total) : '-'}</span></div>
          {att.length ? <>
            <div style={{ marginTop: 6 }}>{att.map((a: any) => (
              <div key={a.code} className="cbar"><span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.farm}</span>
                <div className="track"><div className="fill" style={{ width: `${Math.max(2, a.share * 100)}%` }} /></div>
                <span className="mono" style={{ textAlign: 'right' }}>{Math.round(a.share * 100)}%</span></div>))}</div>
            <div style={{ fontSize: 17, fontWeight: 700, color: '#f0abfc', marginTop: 6 }}>주 원인 추정: {att[0].farm} ({Math.round(att[0].share * 100)}%)</div>
            <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>① 민원 위치 → ② 그 지점의 예측 농도를 배출원별로 분해 → ③ 농가별로 합쳐 순위</div>
          </> : <div style={{ color: 'var(--text-2)', padding: '18px 0' }}>이 위치는 현재 예측 확산 범위 밖입니다.</div>}
          <div style={{ marginTop: 14 }}>
            <div className="muted" style={{ fontSize: 12, margin: '6px 0 2px' }}>최근 민원</div>
            <table className="t"><tbody>
              {complaints.slice(0, 3).map((c) => <tr key={c.id} onClick={() => onSel(c.id)} style={{ cursor: 'pointer', background: sel.id === c.id ? '#10233a' : undefined }}>
                <td className="mono">{dayjs(c.ts).format('MM-DD HH:mm')}</td><td>{c.place}</td><td>{byFarm(c.attribution?.sources)[0]?.farm ?? '(영향권 밖)'}</td></tr>)}
            </tbody></table>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>기여도 = 민원 지점 예측 농도를 배출원별로 분해 (배출량 가정값)</div>
          </div>
        </div>
      )}
    </div>
  )
}

/** 기여도를 농가 단위로 합친다 (한 농가에 센서·배출원이 둘인 경우) */
function byFarm(src?: any[]) {
  const m: Record<string, { farm: string; code: string; share: number; ppm: number }> = {}
  for (const a of src || []) { const x = (m[a.farm] ||= { farm: a.farm, code: a.code, share: 0, ppm: 0 }); x.share += a.share || 0; x.ppm += a.ppm || 0 }
  return Object.values(m).sort((a, b) => b.share - a.share)
}

const DIRS: [string, number][] = [['북', 0], ['북동', 45], ['동', 90], ['남동', 135], ['남', 180], ['남서', 225], ['서', 270], ['북서', 315]]
/** 가상 실험: 바람이 바뀌면? GPU 계산 시간 vs CFD */
function WhatIf() {
  const { fields, clusters, whatif, set, demo } = useStore()
  const [cid, setCid] = useState(demo.story_cluster)
  const f = fields[cid]
  const [wd, setWd] = useState(225); const [ws, setWs] = useState(3)
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null)
  async function run() {
    setBusy(true); setErr(null)
    try {
      const res = await fetch(`/api/whatif?cluster=${cid}&wd=${wd}&ws=${ws}`); const r = await res.json().catch(() => null)
      if (!res.ok || !r) throw new Error(r?.detail || '가상 실험 계산에 실패했습니다')
      set({ whatif: r })
    } catch (e: any) { setErr(String(e.message || e)) } finally { setBusy(false) }
  }
  const w = whatif && whatif.cluster === cid ? whatif : null
  const sec = w ? w.infer_ms / 1000 : null
  return (
    <div className="panel">
      <h3>가상 실험 — 바람이 바뀌면?
        <select style={{ marginLeft: 'auto' }} value={cid} onChange={(e) => setCid(e.target.value)}>{Object.keys(clusters).sort().map((c) => <option key={c} value={c}>{c} 권역</option>)}</select></h3>
      <div style={{ padding: '10px 16px', display: 'flex', flexDirection: 'column', gap: 10, minHeight: 0, flex: 1 }}>
        <div className="muted" style={{ fontSize: 13 }}>현재 바람 {f ? `${dir16(f.wd)} ${f.ws.toFixed(1)} m/s` : '-'} · 바람이 불어오는 방향을 고르세요</div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>{DIRS.map(([n, d]) => <button key={d} className={wd === d ? 'primary' : ''} style={{ minWidth: 52 }} onClick={() => setWd(d)}>{n}</button>)}</div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}><span className="muted" style={{ fontSize: 13 }}>풍속</span>
          {[1.5, 3, 6].map((v) => <button key={v} className={ws === v ? 'primary' : ''} onClick={() => setWs(v)}>{v} m/s</button>)}
          <button className="primary big" style={{ marginLeft: 'auto' }} disabled={busy} onClick={run}>{busy ? '계산 중…' : 'GPU로 계산'}</button>
          <button onClick={() => set({ whatif: null })}>실시간으로 복귀</button></div>
        {err && <div style={{ color: '#fca5a5', fontSize: 13 }}>{err}</div>}
        <div style={{ borderTop: '1px solid var(--line)', paddingTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '170px 1fr 110px', alignItems: 'center', gap: 10 }}>
            <span>GPU 대리모델 (이 PC)</span>
            <div style={{ height: 14, background: 'var(--panel-2)', borderRadius: 4 }}><div style={{ height: '100%', width: sec != null ? `${Math.max(0.4, (sec / 2400) * 100)}%` : 0, minWidth: sec != null ? 4 : 0, background: '#22c55e', borderRadius: 4 }} /></div>
            <span className="mono" style={{ fontSize: 22, color: '#86efac', textAlign: 'right' }}>{sec != null ? `${sec.toFixed(1)} 초` : '–'}</span>
            <span>CFD (서버 8코어) 풍향 1개</span>
            <div style={{ height: 14, background: '#7c2d12', borderRadius: 4 }} />
            <span className="mono" style={{ fontSize: 22, color: '#fdba74', textAlign: 'right' }}>35~45 분</span>
          </div>
          {w && <div className="muted" style={{ fontSize: 12.5 }}>배출원 {w.sources ?? '-'}곳 × 풍향 {w.n_dirs ?? 1}개(σθ {w.sigma}°)를 한 번에 계산 · CFD = RANS 바람장 + 농도 계산
            {w.infer_ms < 50 && <span style={{ color: '#fde68a' }}> · 이미 계산한 풍향(재사용) — 다른 풍향을 고르면 실제 계산 시간</span>}</div>}
          {w && <div style={{ fontSize: 14 }}>예측 최대 <b className="mono">{w.cmax.toFixed(2)} ppm</b>{w.home_max != null && <> · 주택가 최고 <b className="mono">{fmtPpmU(w.home_max)}</b></>}
            {w.homes_06 > 0 && <span style={{ color: '#fca5a5' }}> · 0.6 ppm↑ 주택 {w.homes_06}동</span>}</div>}
          {sec != null && sec > 0.05 && <div style={{ fontSize: 15, color: '#86efac' }}>CFD(약 40분)보다 약 {Math.round(2400 / sec).toLocaleString()}배 빠름 — 바람이 바뀔 때마다 바로 다시 계산</div>}
          {!w && <div className="muted" style={{ fontSize: 13 }}>풍향·풍속을 고르고 [GPU로 계산]을 누르면 결과가 위 지도에 표시됩니다.</div>}
        </div>
      </div>
    </div>
  )
}

/** 데이터 흐름(실시간) + 레이크하우스(배치) + 2년 기록 */
function DataFlow() {
  const { kpi } = useStore()
  const [lake, setLake] = useState<any>({ runs: [], snapshots: [] }); const tables = useStat('tables', 15000); const monthly = useStat('monthly')
  useEffect(() => { const load = () => fetch('/api/lake/status').then((r) => r.json()).then(setLake).catch(() => {}); load(); const t = setInterval(load, 10000); return () => clearInterval(t) }, [])
  const b = kpi?.bridge || {}; const e = kpi?.engine || {}
  const layer = useMemo(() => {
    const L: Record<string, { rows: number; n: number }> = {}
    tables.forEach((t) => { const x = (L[t.layer] ||= { rows: 0, n: 0 }); x.rows += Number(t.row_count) || 0; x.n++ })
    return L
  }, [tables])
  const man = (v?: number) => (v == null ? '-' : v >= 1e4 ? `${(v / 1e4).toFixed(v >= 1e6 ? 0 : 1)}만` : `${v}`)
  const run = lake.runs?.[0]
  const RT = [
    ['MQTT 수신', `${b.mqtt_in_per_s ?? '-'} 건/s`, `게이트웨이 ${kpi?.gateways ?? '-'}개`, false],
    ['Kafka 적재', `${b.kafka_out_per_s ?? '-'} 건/s`, '토픽 15개', kpi?.kafka_ok === false],
    ['Flink 정제·경보', `${kpi?.msgs_per_s ?? '-'} 건/s`, 'MATCH_RECOGNIZE', false],
    ['GPU 확산', e.device ? `${Math.round((e.infer_ms || 0) + (e.render_ms || 0))} ms` : '정지', '권역 20 · 배출원 101', !e.device],
    ['화면 WebSocket', `${kpi?.latency_ms ?? '-'} ms`, '전 구간 지연', false],
  ] as const
  const opt = {
    backgroundColor: 'transparent', textStyle: { color: '#9fb2cc' }, grid: { left: 40, right: 14, top: 26, bottom: 26 }, tooltip: { trigger: 'axis' },
    legend: { right: 8, top: 0, textStyle: { color: '#9fb2cc', fontSize: 12 } },
    xAxis: { type: 'category', data: monthly.map((m) => dayjs(m.month).format('YY-MM')), axisLine: { lineStyle: { color: '#2a3b57' } }, axisLabel: { fontSize: 11 } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: '#16233a' } }, axisLabel: { fontSize: 11 } },
    series: [{ name: '평균', type: 'line', data: monthly.map((m) => m.nh3_avg), smooth: true, showSymbol: false, lineStyle: { color: '#38bdf8' } },
      { name: '상위 5%', type: 'line', data: monthly.map((m) => m.nh3_p95), smooth: true, showSymbol: false, lineStyle: { color: '#f97316' } }],
  }
  return (
    <div className="panel">
      <h3>데이터 흐름 · 레이크하우스<span className="r">실시간 스트림 / 배치</span></h3>
      <div style={{ padding: '10px 12px 0', display: 'flex', flexDirection: 'column', gap: 10, minHeight: 0, flex: 1 }}>
        <div className="flow">{RT.map(([n, v, sub, bad], i) => <span key={n} style={{ display: 'contents' }}>
          {i > 0 && <span className="arr">→</span>}
          <div className={`node ${bad ? 'bad' : ''}`} style={{ flex: 1, minWidth: 0 }}><div className="n">{n}</div><div className="v">{v}</div><div className="s">{sub}</div></div></span>)}</div>
        <div>
          <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>배치 · Trino → Iceberg (120초 주기)</div>
          <div className="flow">
            <div className="node" style={{ flex: 1 }}><div className="n">TimescaleDB (운영)</div><div className="v">실시간 적재</div></div><span className="arr">→</span>
            <div className="node" style={{ flex: 1, borderColor: '#92400e' }}><div className="n" style={{ color: '#f59e0b' }}>bronze</div><div className="v">{man(layer.bronze?.rows)} 행</div></div><span className="arr">→</span>
            <div className="node" style={{ flex: 1, borderColor: '#475569' }}><div className="n" style={{ color: '#cbd5e1' }}>silver</div><div className="v">{man(layer.silver?.rows)} 행</div></div><span className="arr">→</span>
            <div className="node" style={{ flex: 1, borderColor: '#a16207' }}><div className="n" style={{ color: '#fbbf24' }}>gold</div><div className="v">통계표 {layer.gold?.n ?? '-'}개</div></div>
          </div>
          <div className="muted" style={{ fontSize: 12, marginTop: 5 }}>최근 배치 {run ? `${run.job} · ${Number(run.rows ?? 0).toLocaleString()}행 · ${dayjs(run.finished_at).format('HH:mm:ss')} ${run.status === 'ok' ? '성공' : '오류'}` : '-'} · 스냅샷 {lake.snapshots?.length ?? 0}개 (Iceberg 시간 여행)</div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
          <div style={{ fontSize: 13 }}>2년 기록 · 월별 NH₃ (ppm) <span className="muted" style={{ fontSize: 12 }}>2023-02 ~ 2025-04 · gold.monthly_trend</span></div>
          <div style={{ height: 196 }}><ReactECharts option={opt} style={{ height: '100%' }} notMerge /></div>
        </div>
      </div>
    </div>
  )
}

