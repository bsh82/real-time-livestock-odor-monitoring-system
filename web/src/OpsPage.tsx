import { useEffect, useMemo, useRef, useState } from 'react'
import dayjs from 'dayjs'
import ReactECharts from 'echarts-for-react'
import TopBar from './TopBar'
import MapView, { JEJU_VIEW, ISLAND_ZOOM } from './MapView'
import { useStore, sensorLevel, LEVEL_COLOR, KIND_KO, LEVEL_KO, label, post, Alarm, AlarmGroup, groupAlarms, topFarm, clusterCenter, clusterAlarm, emitterOf } from './store'
import Farm3D from './Farm3D'
import { legendGradient, LEGEND_TICKS_MAIN, fmtPpm, fmtPpmU } from './colormap'
import { dir16 } from './util'
export { dir16 }

const rgb = (l: string) => `rgb(${LEVEL_COLOR[l].join(',')})`
const LV_KO: Record<string, string> = { ok: '정상', caution: '주의', warning: '경보', fault: '점검', off: '미수신' }
const ppmCol = (v?: number | null) => (v == null ? undefined : v >= 1 ? '#fca5a5' : v >= 0.6 ? '#fcd34d' : undefined)
const MPP512 = 78271.52

/** 모니터 1 — 실시간 관제: 권역 목록 + 큰 지도 + 오른쪽(센서 상태 · 경보 · 선택 농가) */
export default function OpsPage() {
  const s = useStore()
  const [view, setView] = useState<any>(null)
  const [follow, setFollow] = useState(true)
  const [farm3d, setFarm3d] = useState<string | null>(null)
  const [zoom, setZoom] = useState(JEJU_VIEW.zoom)
  const f3 = useRef<string | null>(null); f3.current = farm3d
  const followed = useRef(0)

  useEffect(() => { if (s.toast) { const t = setTimeout(() => s.set({ toast: null }), 6000); return () => clearTimeout(t) } }, [s.toast])
  // 자동 추적: 새로 발생한 악취 경보만 한 번씩 따라간다. 3D 를 보고 있으면 지도를 옮기지 않는다(알림만).
  useEffect(() => {
    const a = s.lastNew; if (!a || a.id <= followed.current) return
    followed.current = a.id
    if (follow && !f3.current) focusAlarm(a)
  }, [s.lastNew?.id])

  function focusCluster(cid: string) {
    const c = s.clusters[cid]; const ctr = clusterCenter(cid); if (!c || !ctr) return
    const lons = c.bounds.map((b) => b[0]), lats = c.bounds.map((b) => b[1]); const cos = Math.cos((ctr[1] * Math.PI) / 180)
    const wm = (Math.max(...lons) - Math.min(...lons)) * 111320 * cos, hm = (Math.max(...lats) - Math.min(...lats)) * 111320
    const z = Math.log2(Math.min((1200 * 0.72) / wm, (1000 * 0.72) / hm) * MPP512 * cos)       // 권역 상자가 지도의 약 70 %
    setFarm3d(null); s.set({ selCluster: cid }); setView({ longitude: ctr[0], latitude: ctr[1], zoom: Math.max(11.6, Math.min(14, z)) })
  }
  function focusCode(code: string, zoom = 13.2) {
    const x = s.sources[code]; if (!x) return
    s.set({ selected: code, selCluster: x.cluster }); setView({ longitude: x.lon, latitude: x.lat, zoom })
  }
  function focusAlarm(a: Alarm) {
    if (s.sources[a.code]) focusCode(a.code, 13)
    else { const tf = topFarm(a.code); if (tf) focusCode(tf.code, 12.8); else focusCluster(a.cluster || a.code) }
  }
  // 지도 위 패널 자리 (이름표를 그 밑에 두지 않음)
  const reserve = (w: number, h: number) => [[0, 0, 560, 62], [w - 340, 0, w, 52], [0, h - 250, 330, h], [0, h - 32, w, h]]

  return (
    <div className="app">
      <TopBar title="제주 축산악취 실시간 관제" />
      <div style={{ display: 'grid', gridTemplateColumns: '236px 1fr 440px', gap: 8, padding: 8, minHeight: 0 }}>
        <ClusterList onPick={focusCluster} />
        <div className="panel">
          <MapView view={view} onSensorClick={setFarm3d} onZoom={setZoom} reserve={reserve} />
          <Headline onClick={(cid, code) => { setFarm3d(null); code ? focusCode(code, 12.8) : focusCluster(cid) }} />
          <div style={{ position: 'absolute', top: 12, right: 12, display: 'flex', gap: 6 }}>
            <button onClick={() => { setFarm3d(null); setView({ ...JEJU_VIEW }) }}>제주 전역</button>
            <button onClick={() => focusCluster(s.demo.west_cluster)}>서부 밀집지</button>
            <button className={follow ? 'primary' : ''} onClick={() => setFollow(!follow)}>자동 추적 {follow ? 'ON' : 'OFF'}</button>
          </div>
          {s.toast && <div className={`toast ${s.toast.kind === 'info' ? 'info' : s.toast.kind === 'caution' ? 'caution' : ''}`}>{s.toast.text}</div>}
          <Legend island={zoom < ISLAND_ZOOM} />
          <Provenance />
          {farm3d && <Farm3D key={farm3d} code={farm3d} onClose={() => setFarm3d(null)} />}
        </div>
        <div style={{ display: 'grid', gridTemplateRows: '92px 1fr 372px', gap: 8, minHeight: 0 }}>
          <Counters />
          <AlarmPanel onFocus={(a) => { setFarm3d(null); focusAlarm(a) }} onOpen3d={(code) => { focusCode(code); setFarm3d(code) }} />
          <SelectedFarm onOpen3d={(code) => setFarm3d(code)} />
        </div>
      </div>
    </div>
  )
}

/** 왼쪽: 권역 20곳 — 상태 색 · 예측 최대 농도 · 대표 농가 · 바람. 누르면 그 권역으로 */
function ClusterList({ onPick }: { onPick: (cid: string) => void }) {
  const { clusters, fields, alarms, sources, selCluster } = useStore()
  const cst = useMemo(() => clusterAlarm(alarms, sources), [alarms, sources])
  const ids = Object.keys(clusters).sort()
  const nSrc = (cid: string) => (clusters[cid].members || []).filter((c) => sources[c] && sources[c].role !== 'measure').length
  return (
    <div className="panel">
      <h3>권역 관리<span className="r">{ids.length}개 권역</span></h3>
      <div className="scroll">
        {ids.map((cid) => {
          const f = fields[cid]; const st = cst[cid]; const lv = st?.level ?? 'ok'; const tf = topFarm(cid)
          return (
            <div key={cid} className={`crow ${selCluster === cid ? 'sel' : ''}`} onClick={() => onPick(cid)}>
              <span className="cstat" style={{ background: lv === 'ok' ? '#2a3b57' : rgb(lv) }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="c1"><b className="mono">{cid}</b><span className="muted">배출원 {nSrc(cid)}</span>
                  {st?.n ? <span className={`pill ${lv}`}>{LEVEL_KO[lv]} {st.n}</span> : null}
                  <span className="mono cv" style={{ color: ppmCol(f?.cmax) }}>{f ? fmtPpm(f.cmax) : '-'}</span></div>
                <div className="c2">{tf?.farm ?? '-'}{f ? ` · ${dir16(f.wd)} ${f.ws.toFixed(1)} m/s` : ''}</div>
              </div>
            </div>
          )
        })}
      </div>
      <div className="muted" style={{ fontSize: 11.5, padding: '6px 12px', borderTop: '1px solid var(--line)' }}>오른쪽 숫자 = 권역 예측 최대 농도 (ppm)</div>
    </div>
  )
}

/** 지도 왼쪽 위 한 문장: 지금 냄새가 가장 짙은 곳 */
function Headline({ onClick }: { onClick: (cid: string, code?: string) => void }) {
  const { fields } = useStore()
  const f = Object.values(fields).sort((a, b) => b.cmax - a.cmax)[0]
  if (!f) return null
  const tf = topFarm(f.cluster)
  return (
    <div className="overlay headline" onClick={() => onClick(f.cluster, tf?.code)}>
      <span className="muted" style={{ fontSize: 14 }}>지금 냄새가 가장 짙은 곳</span>
      <b>{tf?.farm ?? f.cluster} 일대</b><span className="muted" style={{ fontSize: 13 }}>{f.cluster}</span>
      <span className="muted">·</span><span>{dir16(f.wd)} {f.ws.toFixed(1)} m/s</span>
    </div>
  )
}

function Legend({ island }: { island: boolean }) {
  return (
    <div className="overlay legend">
      <div style={{ color: 'var(--text)' }}>예측 NH₃ 농도 · 지상 2 m (ppm)</div>
      <div style={{ height: 10, borderRadius: 3, marginTop: 6, background: legendGradient(2.2) }} />
      <div className="mono" style={{ position: 'relative', height: 16, fontSize: 12 }}>
        {LEGEND_TICKS_MAIN.map((t) => <span key={t.v} style={{ position: 'absolute', left: `${t.pos}%`, transform: 'translateX(-50%)' }}>{t.v}</span>)}</div>
      <div>흰 선 = 주택가 예측 0.6 ppm 주의 · 1 ppm 경보</div>
      <div className="dots">
        {[['ok', '정상'], ['caution', '주의'], ['warning', '경보'], ['fault', '점검'], ['off', '미수신']].map(([k, l]) => <span key={k}><span className="dot" style={{ background: rgb(k) }} />{l}</span>)}
        <span><span className="dot" style={{ background: '#d946ef' }} />민원</span>
        <span><span style={{ display: 'inline-block', width: 12, height: 9, border: '1.5px solid #6e87aa', marginRight: 6 }} />권역(계산 영역)</span>
      </div>
      <div className="muted" style={{ marginTop: 4 }}>농장 점을 누르면 3D 확산</div>
      {island && <div style={{ marginTop: 4, color: '#fde68a' }}>섬 전체 보기: 원 = 권역 최고 농도 · 꼬리 = 바람 방향 (모식, 확대하면 실제 계산 결과)</div>}
    </div>
  )
}

function Provenance() {
  const { kpi, fields } = useStore()
  const sigma = Object.values(fields)[0]?.sigma ?? 3
  const text = `자료: 제주 센서 124곳 실측(2024-11) ×${kpi?.clock?.speed ?? 60} 재생 · 바람 JMA MSM · 배출량 가정값 · 데이터 이상 센서 9개 확산 제외  |  모델: U-FNO+F 바람 → FNO 농도 (CFD 학습 D = νₜ/0.7, 외부 시험 로그 오차 0.33 · R² 0.70) · 풍향 변동 σθ ${sigma}° · 중립·정상 상태 가정  |  지도 © CARTO © OpenStreetMap contributors`
  return <div className="provenance" title={text}>{text}</div>
}

/** 센서 상태 4칸 */
function Counters() {
  const { sources, sensors, alarms, status } = useStore()
  const n = useMemo(() => {
    const c: Record<string, number> = { ok: 0, caution: 0, warning: 0, fo: 0 }
    Object.keys(sources).forEach((code) => { const l = sensorLevel(code); if (l === 'fault' || l === 'off') c.fo++; else c[l]++ })
    return c
  }, [sources, sensors, alarms, status])
  const T = [['ok', '정상', n.ok], ['caution', '주의', n.caution], ['warning', '경보', n.warning], ['off', '점검·미수신', n.fo]] as const
  return (
    <div className="panel">
      <div className="muted" style={{ fontSize: 12, padding: '5px 10px 0' }}>센서 {Object.keys(sources).length}개 상태</div>
      <div className="counters">
        {T.map(([k, l, v]) => <div key={k}><span className="n" style={{ color: k === 'off' ? '#94a3b8' : rgb(k) }}>{v}</span><span className="l">{l}</span></div>)}
      </div>
    </div>
  )
}

const STEPS = ['발생', '확인', '조치', '해제']
function Stepper({ state }: { state: string }) {
  const cur = state === 'active' ? 0 : state === 'ack' ? 1 : 2
  return (
    <div className="stepper">
      {STEPS.map((st, i) => <span key={st} style={{ display: 'contents' }}>
        {i > 0 && <span className="bar" />}
        <span className={`s ${i < cur ? 'done' : i === cur ? 'cur' : ''}`}><i />{st}</span>
      </span>)}
    </div>
  )
}

/** 경보: 악취 경보는 농가별 카드(단계 표시 + 다음 단계 버튼 하나), 설비·센서 점검은 아래에 센서별로 접어 둔다 */
function AlarmPanel({ onFocus, onOpen3d }: { onFocus: (a: Alarm) => void; onOpen3d: (code: string) => void }) {
  const { alarms, sources } = useStore()
  const { odor, maint } = useMemo(() => groupAlarms(alarms, sources), [alarms, sources])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const nW = odor.filter((g) => g.level === 'warning').length, nC = odor.filter((g) => g.level === 'caution').length
  async function next(g: AlarmGroup) {
    setBusy(g.key)
    try {
      if (g.state === 'active') await Promise.all(g.alarms.filter((a) => a.state === 'active').map((a) => post(`/api/alarms/${a.id}/ack`)))
      else if (g.state === 'ack') await Promise.all(g.ids.map((id) => post(`/api/alarms/${id}/action`, {})))
      else await Promise.all(g.ids.map((id) => post(`/api/alarms/${id}/clear`)))
    } finally { setBusy(null) }
  }
  const code3d = (g: AlarmGroup) => emitterOf(sources[g.code] ? g.code : topFarm(g.code)?.code)
  const target = (g: AlarmGroup) => { const c = g.action?.split(':')[1]; return c ? label(c) : g.farm.replace(' 일대', '') }
  return (
    <div className="panel">
      <h3>악취 경보 {nW > 0 && <span className="pill warning">경보 {nW}</span>}{nC > 0 && <span className="pill caution">주의 {nC}</span>}
        <span className="r">발생 → 확인 → 저감 조치 → 해제</span></h3>
      <div className="scroll">
        {odor.length === 0 && <div style={{ padding: 28, textAlign: 'center', color: '#86efac', fontSize: 15 }}>진행 중인 악취 경보 없음<div className="muted" style={{ fontSize: 13, marginTop: 4 }}>센서 124개 감시 중</div></div>}
        {odor.map((g) => (
          <div key={g.key} className={`alarm-card ${g.level} ${g.isNew ? 'new' : ''}`} onClick={() => onFocus(g.alarms[0])}>
            <div className="t"><span className={`pill ${g.level}`}>{LEVEL_KO[g.level]}</span><b>{KIND_KO[g.kind]}</b><span>{g.farm}</span>
              {g.ids.length > 1 && <span className="muted" style={{ fontSize: 12 }}>센서 {g.ids.length}개</span>}
              <span className="time">{dayjs(g.last).format('HH:mm')}</span></div>
            <div className="d" title={g.alarms[0].detail}>{g.alarms[0].detail}</div>
            {g.state === 'action' && <div className="act">저감시설 가동 → {target(g)} · 농도 감소 확인 중</div>}
            <div className="b" onClick={(e) => e.stopPropagation()}>
              <Stepper state={g.state} />
              <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
                <button className="primary" disabled={busy === g.key} onClick={() => next(g)}>{g.state === 'active' ? '확인' : g.state === 'ack' ? '저감 조치' : '해제'}</button>
                {code3d(g) && <button onClick={() => onOpen3d(code3d(g)!)}>3D</button>}
              </span>
            </div>
          </div>
        ))}
      </div>
      {maint.length > 0 && <div className="maint">
        <div className="h" onClick={() => setOpen(!open)}><span className="pill fault">점검</span>설비·센서 점검 {maint.length}개 센서<span className="muted" style={{ marginLeft: 'auto' }}>{open ? '접기 ▾' : '펼치기 ▸'}</span></div>
        <div style={{ maxHeight: open ? 190 : undefined, overflowY: open ? 'auto' : undefined }}>
          {(open ? maint : maint.slice(0, 2)).map((m) => (
            <div key={m.code} className="row">
              <span style={{ whiteSpace: 'nowrap' }}>{m.text}</span>
              <span className="muted" style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label(m.code)}{sources[m.code]?.suspect_reason ? ` · ${sources[m.code].suspect_reason}` : ''}</span>
              {m.alarms.some((a) => a.state === 'active') && <button onClick={() => Promise.all(m.alarms.filter((a) => a.state === 'active').map((a) => post(`/api/alarms/${a.id}/ack`)))}>확인</button>}
              <button onClick={() => Promise.all(m.alarms.map((a) => post(`/api/alarms/${a.id}/clear`)))}>해제</button>
            </div>
          ))}
        </div>
      </div>}
    </div>
  )
}

/** 선택 농가: 센서 실측 NH₃ · 12시간 추이 · 바람 · 권역 주택 최고 예측 */
function SelectedFarm({ onOpen3d }: { onOpen3d: (code: string) => void }) {
  const { selected, sources, sensors, history, fields, alarms, status } = useStore()
  // 선택이 없을 때 보여 줄 농가: 맨 위 악취 경보의 배출원 (한 번 정하면 선택하기 전까지 유지)
  const latch = useRef<string | null>(null)
  if (!selected && !latch.current && Object.keys(sources).length) {
    const g = groupAlarms(alarms, sources).odor[0]
    latch.current = emitterOf(g ? (sources[g.code] ? g.code : topFarm(g.code)?.code) : null) ?? Object.values(sources).find((x) => x.role !== 'measure' && !x.suspect)?.code ?? Object.keys(sources)[0]
  }
  const code = selected || latch.current || ''
  const src = sources[code]; const sv = sensors[code]
  const [hist, setHist] = useState<any[]>([])
  useEffect(() => { if (!code) return; const load = () => fetch(`/api/history/${code}?hours=12`).then((r) => r.json()).then((x) => Array.isArray(x) && setHist(x)).catch(() => {}); load(); const t = setInterval(load, 10000); return () => clearInterval(t) }, [code])
  if (!src) return <div className="panel" />
  const lv = sensorLevel(code); const f = fields[src.cluster]
  const mem = f?.members.find((m) => m.code === code)
  const thr = sv?.thr
  const live = history[code] || []
  const opt = {
    backgroundColor: 'transparent', textStyle: { color: '#9fb2cc' }, grid: { left: 40, right: 14, top: 12, bottom: 22 }, tooltip: { trigger: 'axis' },
    xAxis: { type: 'time', axisLine: { lineStyle: { color: '#2a3b57' } }, axisLabel: { fontSize: 11, hideOverlap: true, formatter: (v: number) => dayjs(v).format('HH:mm') } },
    yAxis: { type: 'value', min: 0, max: (v: any) => Math.ceil(Math.max(v.max, (thr ?? 0) * 1.1, 1)), splitLine: { lineStyle: { color: '#16233a' } }, axisLabel: { fontSize: 11 } },
    series: [
      { name: '10분 평균', type: 'line', showSymbol: false, data: hist.map((h) => [h.window_start, h.nh3_avg]), lineStyle: { color: '#38bdf8' }, areaStyle: { color: 'rgba(56,189,248,.10)' },
        markLine: thr ? { symbol: 'none', silent: true, data: [{ yAxis: thr }], lineStyle: { color: '#ef4444', type: 'dashed' }, label: { formatter: `기준 ${thr.toFixed(1)}`, color: '#fca5a5', fontSize: 11 } } : undefined },
      { name: '실시간', type: 'line', showSymbol: false, data: live.map((p) => [p.ts, p.v]), lineStyle: { color: '#fbbf24', width: 1.2 } },
    ],
  }
  const hm = f?.home_max
  return (
    <div className="panel">
      <h3><b style={{ fontSize: 16 }}>{src.farm}</b><span className="muted" style={{ fontSize: 12 }}>{src.loc} · {src.cluster} 권역</span>
        <button className="primary" style={{ marginLeft: 'auto' }} onClick={() => onOpen3d(emitterOf(code) ?? code)}>3D 보기</button></h3>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '8px 14px 0' }}>
        <span className="muted" style={{ fontSize: 13 }}>센서 실측 NH₃</span>
        <span className="mono" style={{ fontSize: 28, fontWeight: 700, color: lv === 'ok' ? '#f1f5f9' : rgb(lv) }}>{status[code] === 'offline' ? '두절' : sv ? sv.nh3.toFixed(1) : '–'}</span>
        <span className="muted">ppm</span>
        {thr ? <span className="muted" style={{ fontSize: 12 }}>이 센서 기준 {thr.toFixed(1)}</span> : null}
        <span className={`pill ${lv === 'off' ? 'fault' : lv}`} style={{ marginLeft: 'auto' }}>{LV_KO[lv]}</span>
      </div>
      <div className="muted" style={{ fontSize: 12, padding: '2px 14px 0', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {src.suspect ? <span style={{ color: '#c4b5fd' }}>데이터 이상 센서 ({src.suspect_reason}) · 확산 계산 제외</span>
          : <>최근 12시간 · <span style={{ color: '#38bdf8' }}>━</span> 10분 평균(Flink) · <span style={{ color: '#fbbf24' }}>━</span> 실시간{thr ? <> · <span style={{ color: '#ef4444' }}>┅</span> 경보 기준</> : null}</>}
      </div>
      <div style={{ height: 196 }}><ReactECharts option={opt} style={{ height: '100%' }} notMerge lazyUpdate /></div>
      <div className="kv" style={{ paddingBottom: 10 }}>
        <div><div className="l">바람 (10 m)</div><div className="v">{mem ? `${dir16(mem.wd)} ${mem.ws.toFixed(1)} m/s` : f ? `${dir16(f.wd)} ${f.ws.toFixed(1)} m/s` : '-'}</div></div>
        <div><div className="l">{hm != null ? '권역 주택가 최고 예측' : '권역 예측 최대'}</div>
          <div className="v" style={{ color: ppmCol(hm ?? f?.cmax) }}>{hm != null ? fmtPpmU(hm) : f ? `${f.cmax.toFixed(2)} ppm` : '-'}
            {f && f.homes_06 > 0 && <span style={{ color: '#fca5a5', fontSize: 12, marginLeft: 6 }}>0.6 ppm↑ {f.homes_06}동</span>}</div></div>
      </div>
    </div>
  )
}
