import { create } from 'zustand'

export type Sensor = { code: string; ts_ms: number; nh3: number; temp: number; hum: number; ws: number; wd: number; qc: string; role: string; cluster: string; thr: number; sent_ms?: number }
export type Field = {
  cluster: string; version: number; ts_ms: number; png: string; wind: string; bounds: number[][]; wd: number; ws: number; q_total: number
  cmax: number; cmax_lonlat: number[]; area_06: number; area_10: number; homes_06: number; homes_10: number; schools_06: number; care_06: number
  homes_total: number; home_max?: number; infer_ms: number; sigma?: number; members: { code: string; q: number; wd: number; ws: number }[]; measures: { code: string; pred: number }[]
}
export type Alarm = { id: number; code: string; cluster: string; kind: string; level: string; state: string; first_ts: string; last_ts: string; value: number; detail: string; action?: string; _new?: boolean }
export type Complaint = { id: number; ts: string; lat: number; lon: number; place: string; body: string; intensity: number; status: string; attribution: any }
export type Source = { code: string; farm: string; loc: string; place: string; pos: string; role: string; research: string; livestock: string; cluster: string; lat: number; lon: number; suspect?: boolean; suspect_reason?: string }
export type Cluster = { id: string; members: string[]; bounds: number[][]; receptors: number; homes: number; schools: number; care: number }
export type Toast = { text: string; kind: 'warning' | 'caution' | 'info' }
/** 시연 설정 (서버 data/demo_config.json → /api/meta.demo). 실제 센서 코드는 화면 코드에 두지 않는다 */
export type Demo = { spike_code?: string; offline_code?: string; story_cluster: string; west_cluster: string }

type St = {
  sources: Record<string, Source>; clusters: Record<string, Cluster>; sensors: Record<string, Sensor>; fields: Record<string, Field>
  status: Record<string, string>; alarms: Record<number, Alarm>; complaints: Complaint[]; kpi: any; selected: string | null; selCluster: string | null
  toast: Toast | null; history: Record<string, { ts: number; v: number }[]>; whatif: any | null; connected: boolean
  lastNew: Alarm | null                 // 자동 추적 대상: 마지막으로 새로 발생한 악취 경보 (데이터 이상 센서 제외)
  demo: Demo
  set: (p: Partial<St>) => void
}
export const useStore = create<St>((set) => ({
  sources: {}, clusters: {}, sensors: {}, fields: {}, status: {}, alarms: {}, complaints: [], kpi: {}, selected: null, selCluster: null,
  toast: null, history: {}, whatif: null, connected: false, lastNew: null, demo: { story_cluster: 'C01', west_cluster: 'C02' }, set: (p) => set(p),
}))

export const LEVEL_RANK: Record<string, number> = { warning: 3, caution: 2, fault: 1 }
export const KIND_KO: Record<string, string> = { EMISSION_HIGH: '배출 이상', IMPACT: '확산 영향', COMM_LOSS: '통신 두절', SENSOR_RANGE: '측정 범위 이탈', SENSOR_STUCK: '센서 고착' }
export const STATE_KO: Record<string, string> = { active: '발생', ack: '확인', action: '조치 중', cleared: '해제' }
export const LEVEL_KO: Record<string, string> = { warning: '경보', caution: '주의', fault: '점검' }
const ODOR_KINDS = new Set(['EMISSION_HIGH', 'IMPACT'])

/** 데이터 이상 센서: 메타의 suspect(서버 config.SUSPECT) 또는 측정 범위 이탈·고착 경보가 열려 있는 센서 */
export function isSuspect(code: string): boolean {
  const s = useStore.getState()
  if (s.sources[code]?.suspect) return true
  return Object.values(s.alarms).some((a) => a.code === code && (a.kind === 'SENSOR_RANGE' || a.kind === 'SENSOR_STUCK'))
}

export function sensorLevel(code: string): 'warning' | 'caution' | 'fault' | 'ok' | 'off' {
  const s = useStore.getState()
  if (s.status[code] === 'offline' || !s.sensors[code]) return 'off'
  if (isSuspect(code)) return 'fault'
  let best = 0, lv: any = 'ok'
  for (const a of Object.values(s.alarms)) {
    if (a.code !== code) continue
    const r = LEVEL_RANK[a.level] || 0
    if (r > best) { best = r; lv = a.level }
  }
  return lv
}
export const LEVEL_COLOR: Record<string, number[]> = { ok: [34, 197, 94], caution: [245, 158, 11], warning: [239, 68, 68], fault: [167, 139, 250], off: [100, 116, 139] }

/** 권역에서 배출량이 가장 큰 (데이터 이상이 아닌) 농가. 모두 이상이면 그중 가장 큰 곳 */
export function topFarm(cid: string): { farm: string; code: string } | null {
  const s = useStore.getState(); const f = s.fields[cid]
  const ms = f ? [...f.members].sort((a, b) => b.q - a.q) : []
  const m = ms.find((x) => !isSuspect(x.code)) ?? ms[0]
  const mem = s.clusters[cid]?.members || []
  const code = m?.code ?? mem.find((c) => !isSuspect(c)) ?? mem[0]
  return code && s.sources[code] ? { farm: s.sources[code].farm, code } : null
}

/** 같은 농가의 배출원 코드 (경계·측정 센서면 같은 농가 배출원으로 바꿈) */
export function emitterOf(code?: string | null): string | undefined {
  const s = useStore.getState(); const x = code ? s.sources[code] : undefined
  if (!x) return undefined
  if (x.role !== 'measure') return code!
  return Object.values(s.sources).find((y) => y.farm === x.farm && y.role !== 'measure')?.code ?? code!
}

export type AlarmGroup = { key: string; farm: string; kind: string; level: string; state: string; ids: number[]; alarms: Alarm[]; code: string; last: string; first: number; isNew: boolean; action?: string }
export type MaintGroup = { code: string; alarms: Alarm[]; rank: number; text: string }
/** 경보를 '악취 경보(농가 × 종류)'와 '설비·센서 점검(센서별)'으로 나눈다 */
export function groupAlarms(alarms: Record<number, Alarm>, sources: Record<string, Source>) {
  const odor: Record<string, AlarmGroup> = {}; const mt: Record<string, MaintGroup> = {}
  for (const a of Object.values(alarms)) {
    if (!ODOR_KINDS.has(a.kind) || a.level === 'fault' || (a.kind !== 'IMPACT' && isSuspect(a.code))) {
      const m = (mt[a.code] ||= { code: a.code, alarms: [], rank: 0, text: '' }); m.alarms.push(a)
      const r = a.kind === 'COMM_LOSS' ? 3 : a.kind.startsWith('SENSOR') ? 2 : 1
      if (r > m.rank) { m.rank = r; m.text = r === 1 ? '데이터 이상 · 배출 이상 보류' : KIND_KO[a.kind] || a.kind }
      continue
    }
    const farm = a.kind === 'IMPACT' ? `${topFarm(a.code)?.farm ?? a.code} 일대` : (sources[a.code]?.farm ?? a.code)
    const key = `${farm}|${a.kind}`
    const g = odor[key] ||= { key, farm, kind: a.kind, level: a.level, state: a.state, ids: [], alarms: [], code: a.code, last: a.last_ts, first: a.id, isNew: false }
    g.ids.push(a.id); g.alarms.push(a); g.isNew ||= !!a._new; g.first = Math.min(g.first, a.id)
    if ((LEVEL_RANK[a.level] || 0) > (LEVEL_RANK[g.level] || 0)) g.level = a.level
    if (a.last_ts > g.last) g.last = a.last_ts
    if (sources[g.code]?.role === 'measure' && sources[a.code]?.role !== 'measure') g.code = a.code      // 대표 코드는 배출원 우선
    if (a.action) g.action = a.action
  }
  for (const g of Object.values(odor)) {
    const st = g.alarms.map((a) => a.state)
    g.state = st.includes('active') ? 'active' : st.includes('ack') ? 'ack' : 'action'
  }
  // 순서 고정: 등급 → 먼저 생긴 경보가 위 (갱신 시각으로 정렬하면 카드가 몇 초마다 자리를 바꾼다)
  const list = Object.values(odor).sort((a, b) => (LEVEL_RANK[b.level] || 0) - (LEVEL_RANK[a.level] || 0) || b.first - a.first)
  const maint = Object.values(mt).sort((a, b) => b.rank - a.rank || Math.max(...b.alarms.map((x) => x.id)) - Math.max(...a.alarms.map((x) => x.id)))
  return { odor: list, maint }
}

/** 권역 상태: 가장 높은 악취 경보 등급과 경보 수 */
export function clusterAlarm(alarms: Record<number, Alarm>, sources: Record<string, Source>) {
  const out: Record<string, { level: string; n: number }> = {}
  for (const a of Object.values(alarms)) {
    if (!ODOR_KINDS.has(a.kind) || a.level === 'fault' || (a.kind !== 'IMPACT' && isSuspect(a.code))) continue
    const cid = a.kind === 'IMPACT' ? a.code : sources[a.code]?.cluster ?? a.cluster
    const o = (out[cid] ||= { level: 'ok', n: 0 }); o.n++
    if ((LEVEL_RANK[a.level] || 0) > (LEVEL_RANK[o.level] || 0)) o.level = a.level
  }
  return out
}

let ws: WebSocket | null = null
const lastToast = { who: '', at: 0 }
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
async function getJson(url: string) { const r = await fetch(url); if (!r.ok) throw new Error(`${url} ${r.status}`); return r.json() }

/** 메타(센서·권역) — 엔진이 아직 안 떠서 권역이 비어 있으면 실패로 보고 다시 받는다 */
async function loadMeta() {
  const meta = await getJson('/api/meta')
  if (!meta.clusters?.length) throw new Error('clusters not ready')
  const sources: Record<string, Source> = {}; meta.sources.forEach((s: Source) => (sources[s.code] = s))
  const clusters: Record<string, Cluster> = {}; meta.clusters.forEach((c: Cluster) => (clusters[c.id] = c))
  useStore.getState().set({ sources, clusters, demo: { story_cluster: 'C01', west_cluster: 'C02', ...(meta.demo || {}) } })
}
/** 현재 상태 전체(센서·확산장·경보·민원) — 서버 재기동·시연 초기화 뒤에 화면을 서버와 다시 맞춘다 */
async function loadState() {
  const st = await getJson('/api/state')
  const alarms: Record<number, Alarm> = {}; st.alarms.forEach((a: Alarm) => (alarms[a.id] = a))
  useStore.getState().set({ sensors: st.sensors, fields: st.fields, status: st.status, alarms, complaints: st.complaints, kpi: st.kpi, ...(resetSeen ? { history: {}, whatif: null } : {}) })
  resetSeen = false
}
let resetSeen = false
async function resync() {
  for (let i = 0; ; i++) {
    try { await loadMeta(); await loadState(); return } catch { await sleep(Math.min(5000, 1000 + 500 * i)) }
  }
}

export async function boot() {
  await resync()                                   // 서버가 아직 안 떠 있어도 될 때까지 다시 시도
  connect()
}

let opened = false
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws`)
  ws.onopen = () => { useStore.getState().set({ connected: true }); if (opened) resync(); opened = true }     // 다시 붙으면 상태를 새로 받음
  ws.onclose = () => { useStore.getState().set({ connected: false }); setTimeout(connect, 1500) }
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data); const s = useStore.getState()
    if (m.type === 'sensors') {
      const sensors = { ...s.sensors }; const history = { ...s.history }
      for (const it of m.items as Sensor[]) {
        sensors[it.code] = it
        const h = history[it.code] ? history[it.code].slice(-360) : []
        h.push({ ts: it.ts_ms, v: it.nh3 }); history[it.code] = h
      }
      s.set({ sensors, history })
    } else if (m.type === 'kpi') {
      // 재생 시각이 10분 넘게 뒤로 가면 시연 초기화(-Reset) → 지워진 경보·민원을 버리고 다시 맞춘다
      const prev = s.kpi?.clock?.replay_ms, now = m.data?.clock?.replay_ms
      s.set({ kpi: m.data })
      if (prev && now && now < prev - 10 * 60 * 1000) { resetSeen = true; resync() }
    }
    else if (m.type === 'field') s.set({ fields: { ...s.fields, [m.data.cluster]: m.data } })
    else if (m.type === 'status') s.set({ status: { ...s.status, [m.data.code]: m.data.status } })
    else if (m.type === 'alarm') {
      const a: Alarm = m.data.alarm; const alarms = { ...s.alarms }
      if (a.state === 'cleared') delete alarms[a.id]
      else alarms[a.id] = { ...a, _new: m.data.event === 'new' }
      const patch: any = { alarms }
      // 새 악취 경보(주의·경보)와 통신 두절은 알림 + 소리. 데이터 이상 센서는 알리지 않는다. 같은 농가는 3초 안에 한 번만.
      const odor = (a.level === 'warning' || a.level === 'caution') && ODOR_KINDS.has(a.kind) && (a.kind === 'IMPACT' || !isSuspect(a.code))
      if (m.data.event === 'new' && (odor || a.kind === 'COMM_LOSS')) {
        const who = a.kind === 'IMPACT' ? `${topFarm(a.code)?.farm ?? a.code} 일대` : label(a.code)
        const now = Date.now()
        if (who !== lastToast.who || now - lastToast.at > 3000) {
          patch.toast = { text: `${LEVEL_KO[a.level]} · ${KIND_KO[a.kind] || a.kind} — ${who}`, kind: a.level === 'warning' || a.kind === 'COMM_LOSS' ? 'warning' : 'caution' }
          if (location.pathname.startsWith('/ops')) beep(a.level)
        }
        lastToast.who = who; lastToast.at = now
        if (odor) patch.lastNew = a
      }
      s.set(patch)
    } else if (m.type === 'complaint') {
      const list = [m.data, ...s.complaints.filter((c) => c.id !== m.data.id)].slice(0, 50); s.set({ complaints: list })
    } else if (m.type === 'cmd_ack') {
      const d = m.data; if (d?.action === 'mitigate') s.set({ toast: { text: `게이트웨이 응답 · 저감시설 가동 — ${label(d.code)}`, kind: 'info' } })
    }
  }
}

export function label(code: string) {
  const s = useStore.getState().sources[code]
  if (s) return `${s.farm}`
  return code.startsWith('C') ? `${code} 권역` : code
}

let actx: AudioContext | null = null
function beep(level: string) {
  try {
    actx = actx || new AudioContext(); const o = actx.createOscillator(); const g = actx.createGain()
    o.frequency.value = level === 'warning' ? 880 : 520; o.type = 'square'; g.gain.value = 0.04
    o.connect(g); g.connect(actx.destination); o.start(); o.stop(actx.currentTime + 0.35)
  } catch { /* 소리 실패는 무시 */ }
}

export async function post(url: string, body?: any) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })
  return r.json()
}

/** 권역 중심 좌표 */
export function clusterCenter(cid: string): [number, number] | null {
  const c = useStore.getState().clusters[cid]; if (!c) return null
  return [c.bounds.reduce((a, b) => a + b[0], 0) / 4, c.bounds.reduce((a, b) => a + b[1], 0) / 4]
}
