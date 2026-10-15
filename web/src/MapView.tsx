import { useEffect, useMemo, useRef, useState } from 'react'
import DeckGL from '@deck.gl/react'
import { BitmapLayer, ScatterplotLayer, LineLayer, PolygonLayer, TextLayer } from '@deck.gl/layers'
import { WebMercatorViewport } from '@deck.gl/core'
import Map from 'react-map-gl/maplibre'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { Protocol } from 'pmtiles'
import { layers as baseLayers, namedFlavor } from '@protomaps/basemaps'
import { useStore, sensorLevel, LEVEL_COLOR, Field, topFarm, clusterAlarm } from './store'
import { ppmColor } from './colormap'

/** 배경 지도: 인터넷 없이 — 이 PC 의 제주 PMTiles(OSM 기반 Protomaps, 2026-10-14) + 어두운 테마 + 한글 지명.
 *  파일은 data/basemap/ (git 제외, 받는 법은 README) 에 있고 API 가 /basemap 으로 내준다. 한글은 브라우저 글꼴로 그린다. */
maplibregl.addProtocol('pmtiles', new Protocol().tile)
const STYLE: any = {
  version: 8,
  glyphs: `${location.origin}/basemap/fonts/{fontstack}/{range}.pbf`,
  sprite: `${location.origin}/basemap/sprites/dark`,
  sources: { protomaps: { type: 'vector', url: `pmtiles://${location.origin}/basemap/jeju.pmtiles`, attribution: '© OpenStreetMap contributors · Protomaps' } },
  layers: baseLayers('protomaps', namedFlavor('dark'), { lang: 'ko' }),
}
export const JEJU_VIEW = { longitude: 126.55, latitude: 33.37, zoom: 9.9, pitch: 0, bearing: 0 }
export const ISLAND_ZOOM = 11.2            // 이보다 작으면 섬 전체 보기: 확산 위치를 확대 기호로 표시
const MPP512 = 78271.52                    // deck.gl·MapLibre 512 px 타일 기준 m/px (위도 cos, 2^zoom 으로 나눔)

/** 확산장 PNG 를 미리 받아 두었다가 바꿔서 깜빡임을 없앤다 */
function usePlumeImages(fields: Record<string, Field>, override?: any) {
  const [imgs, setImgs] = useState<Record<string, { img: HTMLImageElement; bounds: number[][] }>>({})
  useEffect(() => {
    const all: Record<string, { png: string; bounds: number[][] }> = {}
    Object.values(fields).forEach((f) => (all[f.cluster] = f))
    if (override) all[override.cluster] = override
    Object.entries(all).forEach(([cid, f]) => {
      if ((imgs[cid] as any)?.src === f.png) return
      const im = new Image(); im.crossOrigin = 'anonymous'
      im.onload = () => setImgs((p) => ({ ...p, [cid]: Object.assign({ img: im, bounds: f.bounds }, { src: f.png }) }))
      im.src = f.png
    })
  }, [fields, override])
  return imgs
}

/** reserve: 지도 위에 겹쳐 놓인 패널 영역 [x0, y0, x1, y1] (px) — 이름표를 그 밑에 두지 않는다 */
type Props = { view?: any; whatif?: any; showParticles?: boolean; showAttribution?: boolean; onPick?: (lonlat: number[]) => void; onSensorClick?: (code: string) => void; onZoom?: (z: number) => void; reserve?: (w: number, h: number) => number[][] }

export default function MapView({ view, whatif, showParticles = true, showAttribution = false, onPick, onSensorClick, onZoom, reserve }: Props) {
  const { fields, sources, sensors, alarms, complaints, selected, selCluster, clusters, status, set } = useStore()
  const [vs, setVs] = useState<any>(view || JEJU_VIEW)
  const [t, setT] = useState(0)
  const box = useRef<HTMLDivElement>(null); const [size, setSize] = useState({ w: 1400, h: 900 })
  useEffect(() => { if (view) setVs((p: any) => ({ ...p, ...view, transitionDuration: 1200 })) }, [view])
  useEffect(() => { const id = setInterval(() => setT((x) => x + 1), 120); return () => clearInterval(id) }, [])
  useEffect(() => {
    const el = box.current!; const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight })); ro.observe(el); return () => ro.disconnect()
  }, [])
  const zq = Math.round(vs.zoom * 4) / 4
  useEffect(() => { onZoom?.(zq) }, [zq])
  const imgs = usePlumeImages(fields, whatif)
  const pts = useMemo(() => Object.values(sources), [sources])
  const pulse = (Math.sin(t / 2.2) + 1) / 2
  const island = vs.zoom < ISLAND_ZOOM
  const hotA = Math.max(0, Math.min(1, (ISLAND_ZOOM - vs.zoom) / 0.6))

  // 섬 전체 보기용 확산 기호: 원 = 권역 최고 농도, 꼬리 = 바람이 불어 가는 방향
  const hot = useMemo(() => Object.values(fields).filter((f) => f.cmax >= 0.05 && f.cmax_lonlat).map((f) => {
    const r = Math.max(10, Math.min(30, 10 + 8 * Math.log10(f.cmax / 0.05)))
    const [lon, lat] = f.cmax_lonlat; const b = ((f.wd + 180) * Math.PI) / 180
    const L = (r + 26) * MPP512 * Math.cos((lat * Math.PI) / 180) / Math.pow(2, zq)
    const tip = [lon + (L * Math.sin(b)) / (111320 * Math.cos((lat * Math.PI) / 180)), lat + (L * Math.cos(b)) / 111320]
    return { f, r, pos: [lon, lat], tip, col: ppmColor(f.cmax) }
  }), [fields, zq])

  // 권역(계산 영역) 상자: 상태 색 (경보 빨강 · 주의 주황 · 평상 회청), 선택 권역은 하늘색 굵게
  const cst = useMemo(() => clusterAlarm(alarms, sources), [alarms, sources])
  const boxCol = (id: string): number[] => (id === selCluster ? [56, 189, 248, 230] : cst[id]?.level === 'warning' ? [239, 68, 68, 210] : cst[id]?.level === 'caution' ? [245, 158, 11, 190] : [110, 135, 170, 120])
  const corner = (c: any) => [Math.min(...c.bounds.map((b: number[]) => b[0])), Math.max(...c.bounds.map((b: number[]) => b[1]))]
  const layers = [
    new PolygonLayer({
      id: 'domains', data: Object.values(clusters), getPolygon: (c: any) => c.bounds, stroked: true, filled: false,
      getLineColor: ((c: any) => boxCol(c.id)) as any, getLineWidth: (c: any) => (c.id === selCluster ? 2.5 : 1.2), lineWidthUnits: 'pixels',
      updateTriggers: { getLineColor: [selCluster, cst], getLineWidth: selCluster },
    }),
    new TextLayer({
      id: 'domain-id', data: Object.values(clusters), getPosition: ((c: any) => corner(c)) as any, getText: (c: any) => c.id,
      getSize: (c: any) => (c.id === selCluster ? 14 : 12), getColor: ((c: any) => { const k = boxCol(c.id); return [k[0], k[1], k[2], 255] }) as any,
      getTextAnchor: 'start', getAlignmentBaseline: 'bottom', getPixelOffset: [2, -2], fontFamily: 'JetBrains Mono, monospace', fontWeight: 600,
      background: true, getBackgroundColor: [8, 13, 22, 200], backgroundPadding: [3, 1], pickable: true,
      onClick: (info: any) => { if (info.object) set({ selCluster: info.object.id }) },
      updateTriggers: { getColor: [selCluster, cst], getSize: selCluster },
    }),
    ...Object.entries(imgs).map(([cid, v]) => new BitmapLayer({ id: `plume-${cid}`, image: v.img, bounds: v.bounds as any, opacity: 0.92 })),
    hotA > 0 && new LineLayer({
      id: 'hot-tail', data: hot, getSourcePosition: (d: any) => d.pos, getTargetPosition: (d: any) => d.tip,
      getColor: (d: any) => [d.col[0], d.col[1], d.col[2], 200], getWidth: 5, widthUnits: 'pixels', opacity: hotA,
    }),
    hotA > 0 && new ScatterplotLayer({
      id: 'hot-disc', data: hot, getPosition: (d: any) => d.pos, getRadius: (d: any) => d.r, radiusUnits: 'pixels',
      getFillColor: (d: any) => [d.col[0], d.col[1], d.col[2], 120], getLineColor: (d: any) => [d.col[0], d.col[1], d.col[2], 230],
      stroked: true, lineWidthMinPixels: 2, opacity: hotA,
    }),
    showAttribution && new LineLayer({
      id: 'attr', data: complaints.slice(0, 1).flatMap((c) => (c.attribution?.sources || []).slice(0, 4).map((s: any) => ({ c, s, src: sources[s.code] }))).filter((d: any) => d.src),
      getSourcePosition: (d: any) => [d.c.lon, d.c.lat], getTargetPosition: (d: any) => [d.src.lon, d.src.lat],
      getColor: [232, 121, 249, 210], getWidth: (d: any) => 1.5 + 8 * d.s.share, widthUnits: 'pixels',
    }),
    new ScatterplotLayer({
      id: 'rings', data: pts.filter((p) => { const l = sensorLevel(p.code); return l === 'warning' || l === 'caution' }),
      getPosition: (p: any) => [p.lon, p.lat], getRadius: 9 + 14 * pulse, radiusUnits: 'pixels', stroked: true, filled: false,
      getLineColor: ((p: any) => [...LEVEL_COLOR[sensorLevel(p.code)], 230 * (1 - pulse) + 25]) as any, lineWidthMinPixels: 2,
      updateTriggers: { getRadius: t, getLineColor: [t, alarms] },
    }),
    new ScatterplotLayer({
      id: 'sensors', data: pts, pickable: true, getPosition: (p: any) => [p.lon, p.lat],
      getRadius: (p: any) => (p.role === 'measure' ? 3.5 : 5.5) + (p.code === selected ? 3 : 0), radiusUnits: 'pixels',
      getFillColor: ((p: any) => (p.role === 'measure' ? [15, 23, 42, 255] : [...LEVEL_COLOR[sensorLevel(p.code)], 235])) as any,
      getLineColor: ((p: any) => (p.role === 'measure' ? [...LEVEL_COLOR[sensorLevel(p.code)], 255] : p.code === selected ? [255, 255, 255, 255] : [8, 12, 20, 255])) as any,
      stroked: true, lineWidthMinPixels: 1.5,
      onClick: (info: any) => { if (!info.object) return; set({ selected: info.object.code, selCluster: info.object.cluster }); onSensorClick?.(info.object.code) },
      updateTriggers: { getFillColor: [alarms, status, sensors], getRadius: selected, getLineColor: [alarms, status, selected] },
    }),
    new ScatterplotLayer({
      id: 'complaints', data: complaints.slice(0, 5), getPosition: (c: any) => [c.lon, c.lat], getRadius: 8, radiusUnits: 'pixels',
      getFillColor: [217, 70, 239, 230], stroked: true, getLineColor: [255, 255, 255, 255], lineWidthMinPixels: 2, pickable: true,
    }),
  ].filter(Boolean)

  const labels = useLabels(vs, size, island, hot, reserve)

  return (
    <div ref={box} style={{ position: 'absolute', inset: 0 }}>
      <DeckGL viewState={vs} controller={{ dragRotate: false, touchRotate: false, keyboard: false } as any} layers={layers as any}
        onViewStateChange={(e: any) => setVs(e.viewState)}
        onClick={(info: any) => { if (onPick && info.coordinate && !info.object) onPick(info.coordinate) }}
        getTooltip={({ object }: any) => tooltip(object)}>
        <Map mapStyle={STYLE} reuseMaps attributionControl={false} localIdeographFontFamily="'Noto Sans KR', sans-serif" />
      </DeckGL>
      {showParticles && <Particles vs={vs} sel={selCluster} />}
      <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none', overflow: 'hidden' }}>
        {labels.map((l) => <div key={l.key} style={{ position: 'absolute', left: l.x, top: l.y, transform: 'translate(-50%, -100%)', padding: '2px 6px', borderRadius: 4,
          background: 'rgba(8,13,22,.84)', color: l.color, fontSize: l.big ? 14 : 13, fontWeight: l.big ? 600 : 500, whiteSpace: 'nowrap' }}>{l.text}</div>)}
      </div>
    </div>
  )
}

type Label = { key: string; text: string; x: number; y: number; color: string; big?: boolean; w: number }
/** 농장 이름표: 농장당 1개, 확대 수준에 따라 후보를 고르고, 화면에서 겹치는 것은 우선순위가 낮은 쪽을 뺀다 */
function useLabels(vs: any, size: { w: number; h: number }, island: boolean, hot: any[], reserve?: (w: number, h: number) => number[][]): Label[] {
  const { sources, sensors, alarms, selected, complaints, status } = useStore()
  const farms = useMemo(() => {
    const g: Record<string, { farm: string; codes: string[]; lon: number; lat: number }> = {}
    Object.values(sources).filter((s) => s.role !== 'measure').forEach((s) => {
      const f = (g[s.farm] ||= { farm: s.farm, codes: [], lon: 0, lat: 0 }); f.codes.push(s.code); f.lon += s.lon; f.lat += s.lat
    })
    return Object.values(g).map((f) => ({ ...f, lon: f.lon / f.codes.length, lat: f.lat / f.codes.length }))
  }, [sources])
  const key = [Math.round(vs.zoom * 8), Math.round(vs.longitude * 2e3), Math.round(vs.latitude * 2e3), size.w, size.h, selected, complaints[0]?.id, Object.keys(alarms).length, Math.round(Date.now() / 5000)].join('|')
  return useMemo(() => {
    const vp = new WebMercatorViewport({ ...vs, width: size.w, height: size.h })
    const culprit = complaints[0]?.attribution?.sources?.[0]?.code
    const selFarm = selected ? sources[selected]?.farm : null
    const cand: { text: string; lon: number; lat: number; pri: number; color: string; big?: boolean; off: number }[] = []
    if (island) {
      ;[...hot].sort((a, b) => b.f.cmax - a.f.cmax).slice(0, 3).forEach((h) => {
        const tf = topFarm(h.f.cluster); if (tf) cand.push({ text: `${tf.farm} 일대`, lon: h.pos[0], lat: h.pos[1], pri: 60 + h.f.cmax, color: '#f1f5f9', big: true, off: h.r + 6 })
      })
    }
    for (const f of farms) {
      const lv = f.codes.map((c) => sensorLevel(c))
      const isSel = f.farm === selFarm, warn = lv.includes('warning'), caut = lv.includes('caution'), cul = culprit && f.codes.includes(culprit)
      const nh3 = Math.max(0, ...f.codes.map((c) => (status[c] === 'offline' ? 0 : sensors[c]?.nh3 ?? 0)))
      let pri = isSel ? 100 : warn ? 90 : caut ? 80 : cul ? 70 : Math.min(50, nh3)
      const show = vs.zoom >= 12.5 || isSel || ((warn || caut || cul) && vs.zoom >= 11) || (island && isSel)
      if (!show) continue
      cand.push({ text: f.farm, lon: f.lon, lat: f.lat, pri, color: warn ? '#fca5a5' : caut ? '#fcd34d' : '#e2e8f0', off: 10 })
    }
    cand.sort((a, b) => b.pri - a.pri)
    const out: Label[] = []; const boxes: number[][] = reserve ? reserve(size.w, size.h).map((r) => [...r]) : []
    const nRes = boxes.length
    for (const c of cand) {
      const [x, y0] = vp.project([c.lon, c.lat]); const y = y0 - c.off
      const w = c.text.length * (c.big ? 14 : 13) + 14, h = 22
      const bx = [x - w / 2 - 4, y - h - 4, x + w / 2 + 4, y + 4]
      if (bx[2] < 0 || bx[0] > size.w || bx[3] < 0 || bx[1] > size.h) continue
      if (boxes.some((b) => !(bx[2] < b[0] || bx[0] > b[2] || bx[3] < b[1] || bx[1] > b[3]))) continue
      boxes.push(bx); out.push({ key: c.text + c.lon, text: c.text, x, y, color: c.color, big: c.big, w })
      if (boxes.length - nRes >= 30) break
    }
    return out
  }, [key, farms, hot])
}

function tooltip(o: any) {
  if (!o) return null
  const st = useStore.getState()
  if (o.farm) {
    const s = st.sensors[o.code]
    const role = o.role === 'measure' ? '측정 지점' : o.role === 'proxy' ? '근사 배출원' : '배출원'
    const sus = o.suspect ? `<br/><span style="color:#c4b5fd">데이터 이상(${o.suspect_reason ?? ''}) — 점검 대상, 확산 계산 제외</span>` : ''
    return { html: `<b>${o.farm}</b> <span style="color:#9fb2cc">${o.loc} · ${role}</span><br/>NH₃ <b>${s ? s.nh3.toFixed(2) : '-'}</b> ppm · 기온 ${s ? s.temp.toFixed(1) : '-'}°C${sus}<br/><span style="color:#6f84a3">클릭하면 3D 확산 보기</span>`, style: { background: '#0d1420', border: '1px solid #2a3b57', fontSize: '13px' } }
  }
  if (o.attribution) return { html: `<b>민원</b> ${o.place || ''}<br/>${o.body}<br/>최대 기여: ${o.attribution?.sources?.[0]?.farm || '-'}`, style: { background: '#0d1420' } }
  return null
}

/** 바람 입자: 군집별 바람 격자(U-FNO 10 m 바람, 100 m 간격)와 권역 평균풍으로 입자를 흘린다 */
function Particles({ vs, sel }: { vs: any; sel: string | null }) {
  const ref = useRef<HTMLCanvasElement>(null)
  const fields = useStore((s) => s.fields)
  const grids = useRef<Record<string, any>>({})
  const vsRef = useRef(vs); const selRef = useRef(sel); selRef.current = sel
  useEffect(() => { vsRef.current = vs; const c = ref.current; if (c) c.getContext('2d')!.clearRect(0, 0, c.width, c.height) }, [vs])
  useEffect(() => {
    Object.values(fields).forEach((f) => {
      if (grids.current[f.cluster]?.src === f.wind) return
      fetch(f.wind).then((r) => r.json()).then((g) => {
        const lons = g.bounds.map((b: number[]) => b[0]), lats = g.bounds.map((b: number[]) => b[1])
        grids.current[f.cluster] = { ...g, src: f.wind, w: Math.min(...lons), e: Math.max(...lons), s: Math.min(...lats), n: Math.max(...lats), wd: f.wd, ws: f.ws }
      }).catch(() => {})
    })
  }, [fields])
  useEffect(() => {
    const c = ref.current!; const ctx = c.getContext('2d')!; let raf = 0
    const N = 450; const P: { x: number; y: number; age: number; life: number }[] = []
    const resize = () => { c.width = c.clientWidth; c.height = c.clientHeight }
    resize(); window.addEventListener('resize', resize)
    // 확대 화면(줌 11 이상): 선택 권역의 바람 격자 안에서만, 가장자리로 갈수록 옅게 / 섬 전체: 가까운 권역의 평균풍
    const sample = (lon: number, lat: number, zoom: number): [number, number, number] | null => {
      let best: any = null, bd = 1e9
      for (const [cid, g] of Object.entries(grids.current)) {
        if (zoom >= 11 && cid !== selRef.current) continue
        if (lon >= g.w && lon <= g.e && lat >= g.s && lat <= g.n) {
          const fx = (lon - g.w) / (g.e - g.w), fy = (g.n - lat) / (g.n - g.s)
          const i = Math.round(fy * (g.ny - 1)) * g.nx + Math.round(fx * (g.nx - 1))
          const edge = Math.min(fx, 1 - fx, fy, 1 - fy); return [g.u[i], g.v[i], Math.min(1, edge / 0.15)]
        }
        const d = Math.hypot((lon - (g.w + g.e) / 2) * 0.84, lat - (g.s + g.n) / 2); if (d < bd) { bd = d; best = g }
      }
      if (!best || zoom >= 11) return null
      const th = best.wd * Math.PI / 180; return [-best.ws * Math.sin(th), -best.ws * Math.cos(th), 1]
    }
    const spawn = (p: any) => { p.x = Math.random() * c.width; p.y = Math.random() * c.height; p.age = 0; p.life = 30 + Math.random() * 40 }
    const loop = () => {
      const v = vsRef.current; const vp = new WebMercatorViewport({ ...v, width: c.width, height: c.height })
      ctx.globalCompositeOperation = 'destination-in'; ctx.fillStyle = 'rgba(0,0,0,0.88)'; ctx.fillRect(0, 0, c.width, c.height)
      ctx.globalCompositeOperation = 'source-over'; ctx.lineWidth = 1
      const mpp = MPP512 * Math.cos(v.latitude * Math.PI / 180) / Math.pow(2, v.zoom)    // m / px
      const k = 4.5 / mpp                                                          // 화면 이동 속도 (예전과 같은 값)
      while (P.length < N) { const p: any = {}; spawn(p); P.push(p) }
      for (const p of P) {
        const ll = vp.unproject([p.x, p.y]); const w = sample(ll[0], ll[1], v.zoom)
        if (!w || p.age++ > p.life) { spawn(p); continue }
        const nx = p.x + w[0] * k, ny = p.y - w[1] * k, sp = Math.hypot(w[0], w[1])
        ctx.strokeStyle = `rgba(${170 + sp * 10},225,255,${Math.min(0.28, 0.1 + sp / 30) * w[2]})`
        ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(nx, ny); ctx.stroke(); p.x = nx; p.y = ny
        if (p.x < 0 || p.y < 0 || p.x > c.width || p.y > c.height) spawn(p)
      }
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => { cancelAnimationFrame(raf); window.removeEventListener('resize', resize) }
  }, [])
  return <canvas ref={ref} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }} />
}
