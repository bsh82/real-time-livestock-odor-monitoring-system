import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'
import { ppmColor, fmtPpm, fmtPpmU, legendGradient, LEGEND_TICKS_MAIN } from './colormap'
import { dir16 } from './util'

/** 농장 1곳의 3D 확산 보기: 지형(음영기복 + 지상 2 m 농도), 건물, 등농도면(0~50 m), U-FNO 바람 유선과 흐르는 입자.
 *  20초마다 새 자료를 받되, 재질(셰이더)은 처음 만든 것을 계속 쓰고 형상·텍스처만 바꿔서 갱신할 때 멈춤·깜빡임이 없게 한다. */
type Wind = { wd: number; ws?: number }
type Cam = { az: number; el?: number; dist?: number; tx?: number; ty?: number; rotate?: boolean }    // 카메라 방위(북 0°, 시계 방향)·고도각·거리·바라보는 점(동·북 m)
type Props = { code: string; onClose: () => void; wind?: Wind | null; cam?: Cam | null }
const BLD_COL: Record<string, string> = { home: '#e2e8f0', exposed: '#ef4444', school: '#60a5fa', care: '#a78bfa', barn: '#b7791f', other: '#5b6b82' }
const ISO_OPACITY = [0.17, 0.28, 0.45]
const EXAG = 3                                         // 높이 과장 (고정)

async function fetchScene(code: string, wind?: Wind | null) {
  const q = new URLSearchParams({ code }); if (wind) { q.set('wd', String(wind.wd)); if (wind.ws != null) q.set('ws', String(wind.ws)) }
  const r = await fetch(`/api/farm3d?${q}`)
  const text = await r.text(); let j: any = null
  try { j = JSON.parse(text) } catch { /* 본문이 JSON 이 아님 */ }
  if (!r.ok || !j) throw new Error(j?.detail || '확산 엔진에 연결하는 중입니다. 잠시 후 다시 시도합니다.')
  const tex = await new THREE.TextureLoader().loadAsync(j.texture)        // 텍스처를 먼저 받아 두어 장면 교체 때 검은 화면이 없게
  tex.colorSpace = THREE.SRGBColorSpace; tex.anisotropy = 8
  return { d: j, tex }
}

export default function Farm3D({ code, onClose, wind, cam }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const ctx = useRef<any>(null)
  const [scene, setScene] = useState<{ d: any; tex: THREE.Texture } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [show, setShow] = useState<Record<string, boolean>>({ iso: true, wind: true, bld: true, ratio: false })
  const [rotate, setRotate] = useState(cam?.rotate ?? true)

  // 자료: 처음 + 20초마다. 바람·배출량·최대 농도가 그대로면 장면을 다시 만들지 않는다.
  useEffect(() => {
    let alive = true; let sig = ''
    const load = async () => {
      setLoading(true)
      try {
        const s = await fetchScene(code, wind)
        const nsig = [s.d.code, s.d.wd, s.d.ws, s.d.q_total, s.d.cmax].join('|')
        if (!alive) { s.tex.dispose(); return }
        if (nsig === sig) s.tex.dispose(); else { sig = nsig; setScene(s) }
        setErr(null)
      } catch (e: any) { if (alive) setErr(String(e.message || e)) } finally { if (alive) setLoading(false) }
    }
    load(); const t = setInterval(load, 20000)
    return () => { alive = false; clearInterval(t) }
  }, [code, wind?.wd, wind?.ws])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey); return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // three.js 초기화 (한 번)
  useEffect(() => {
    const el = host.current!
    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio)); renderer.setSize(el.clientWidth, el.clientHeight)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    el.appendChild(renderer.domElement)
    const sc = new THREE.Scene(); sc.background = new THREE.Color('#05080f'); sc.fog = new THREE.Fog('#05080f', 5000, 11000)
    const camera = new THREE.PerspectiveCamera(40, el.clientWidth / el.clientHeight, 5, 40000)
    camera.up.set(0, 0, 1); camera.position.set(-1500, -2300, 1500)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.target.set(0, 0, 80); controls.enableDamping = true; controls.maxPolarAngle = Math.PI * 0.495; controls.autoRotateSpeed = 0.5
    controls.minDistance = 200; controls.maxDistance = 9000
    const hemi = new THREE.HemisphereLight(0xcfe3ff, 0x0d1117, 1.1); hemi.position.set(0, 0, 1); sc.add(hemi)     // z 가 위쪽
    const sun = new THREE.DirectionalLight(0xffffff, 1.5); sun.position.set(-1500, 1800, 2600); sc.add(sun)
    const world = new THREE.Group(); sc.add(world)
    // 재질은 한 번만 만들어 계속 쓴다 (갱신 때 셰이더를 다시 컴파일하지 않게)
    const dot = document.createElement('canvas'); dot.width = dot.height = 32
    const g2 = dot.getContext('2d')!; const grd = g2.createRadialGradient(16, 16, 0, 16, 16, 16)
    grd.addColorStop(0, 'rgba(255,255,255,1)'); grd.addColorStop(0.4, 'rgba(224,242,254,0.8)'); grd.addColorStop(1, 'rgba(224,242,254,0)')
    g2.fillStyle = grd; g2.fillRect(0, 0, 32, 32)
    const mats: Record<string, THREE.Material> = {
      terrain: new THREE.MeshStandardMaterial({ roughness: 0.95, metalness: 0 }),
      lines: new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.26, depthWrite: false }),
      points: new THREE.PointsMaterial({ size: 4.5, sizeAttenuation: false, map: new THREE.CanvasTexture(dot), transparent: true, opacity: 0.95, depthWrite: false, blending: THREE.AdditiveBlending }),
      src: new THREE.MeshStandardMaterial({ color: '#f43f5e', emissive: '#be123c', emissiveIntensity: 0.9 }),
      beam: new THREE.MeshBasicMaterial({ color: '#fb7185', transparent: true, opacity: 0.55 }),
    }
    Object.entries(BLD_COL).forEach(([k, col]) => (mats[`bld:${k}`] = new THREE.MeshStandardMaterial({ color: col, roughness: 0.8, metalness: 0.05, emissive: k === 'exposed' ? '#7f1d1d' : '#000000' })))
    ISO_OPACITY.forEach((o, k) => (mats[`iso:${k}`] = new THREE.MeshPhongMaterial({ transparent: true, opacity: o, side: THREE.DoubleSide, depthWrite: false, shininess: 60 })))
    const arrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(), 420, 0x7dd3fc, 120, 80); arrow.userData.keepShape = true
    const north = new THREE.ArrowHelper(new THREE.Vector3(0, 1, 0), new THREE.Vector3(-1080, -1080, 5), 220, 0xe2e8f0, 60, 40); north.userData.keepShape = true
    sc.add(arrow, north)
    const ro = new ResizeObserver(() => {
      if (!el.clientWidth || !el.clientHeight) return
      renderer.setSize(el.clientWidth, el.clientHeight); camera.aspect = el.clientWidth / el.clientHeight; camera.updateProjectionMatrix()
    })
    ro.observe(el)
    const c: any = { renderer, scene: sc, camera, controls, world, mats, arrow, north, flow: null, clock: new THREE.Clock(), code: null, groundZ: 0, exag: EXAG, tex: null, cam: cam ?? null }
    world.scale.z = EXAG
    ctx.current = c
    let raf = 0
    const loop = () => { controls.update(); stepParticles(c); renderer.render(sc, camera); raf = requestAnimationFrame(loop) }
    raf = requestAnimationFrame(loop)
    return () => {
      cancelAnimationFrame(raf); ro.disconnect(); disposeGeometries(world); c.tex?.dispose()
      Object.values(mats).forEach((m: any) => { m.map?.dispose?.(); m.dispose() }); arrow.dispose(); north.dispose()
      controls.dispose(); renderer.dispose(); renderer.forceContextLoss(); el.removeChild(renderer.domElement); ctx.current = null
    }
  }, [])

  useEffect(() => { if (ctx.current) ctx.current.controls.autoRotate = rotate }, [rotate])
  useEffect(() => { if (ctx.current) ctx.current.cam = cam ?? null }, [cam])
  useEffect(() => {
    const c = ctx.current; if (!c || !scene) return
    disposeGeometries(c.world); c.world.clear(); c.flow = null
    buildScene(c, scene.d, scene.tex)
    applyView(c, show)
  }, [scene])
  useEffect(() => { const c = ctx.current; if (c) applyView(c, show) }, [show])

  const d = scene?.d
  const homeMax = d ? Math.max(-1, ...d.buildings.filter((b: any) => b.k === 'home').map((b: any) => b.c ?? 0)) : -1
  // 바람 세기를 켜면 유선도 켠다 / 유선을 끄면 바람 세기도 끈다
  const tog = (k: string) => setShow(k === 'ratio' ? { ...show, ratio: !show.ratio, wind: show.wind || !show.ratio } : k === 'wind' ? { ...show, wind: !show.wind, ratio: show.wind ? false : show.ratio } : { ...show, [k]: !show[k] })
  return (
    <div style={{ position: 'absolute', inset: 0, zIndex: 30, background: '#05080f', borderRadius: 10, overflow: 'hidden' }}>
      <div ref={host} style={{ position: 'absolute', inset: 0 }} />
      <div style={{ position: 'absolute', left: 14, top: 12, maxWidth: 'calc(100% - 640px)', minWidth: 420, pointerEvents: 'none' }}>
        <div style={{ fontSize: 20, fontWeight: 700, color: '#f1f5f9' }}>{d ? `${d.farm} · 3D 냄새 확산` : '불러오는 중'}</div>
        {d && <div style={{ fontSize: 13, color: '#9fb2cc', marginTop: 2 }}>{d.loc} · 지형·건물·바람 반영
          <span className="badge" style={{ marginLeft: 8 }}>{d.research === 'train' ? 'CFD 학습 농가' : d.research === 'test' ? 'CFD 시험 농가' : 'CFD 없이 예측한 농가'}</span>
          {d.suspect ? <span className="badge" style={{ marginLeft: 6, color: '#c4b5fd', borderColor: '#4c1d95' }}>데이터 이상 센서({d.suspect}) → 확산 계산 제외 · 가정 배출량으로 모양만 표시</span>
            : d.nominal && <span className="badge" style={{ marginLeft: 6, color: '#fcd34d', borderColor: '#78350f' }}>센서값 없음 → 가정 배출량으로 모양만 표시</span>}</div>}
        {d && <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
          <Chip l={d.override ? '바람 (10 m · 지정)' : '바람 (10 m)'} v={`${dir16(d.wd)} ${d.ws.toFixed(1)} m/s`} />
          <Chip l="가장 높은 주택 (이 농장 기여)" v={homeMax >= 0 ? fmtPpmU(homeMax) : '-'} color={homeMax >= 0.6 ? '#fca5a5' : undefined} />
          <Chip l="GPU 계산" v={`${(d.infer_ms / 1000).toFixed(1)}초 · 풍향 ${d.n_dirs ?? 1}개`} sub="CFD는 풍향 1개 35~45분" />
          {d.homes_06 > 0 && <Chip l="0.6 ppm 이상 주택" v={`${d.homes_06}동`} color="#fca5a5" />}
        </div>}
        {err && <div style={{ marginTop: 10, color: '#fca5a5' }}>{err}</div>}
      </div>
      <div style={{ position: 'absolute', right: 12, top: 12, display: 'flex', gap: 6, alignItems: 'center' }}>
        {loading && <span className="muted" style={{ fontSize: 12 }}>갱신 중…</span>}
        {[['iso', '등농도면'], ['wind', '바람 유선'], ['ratio', '바람 세기'], ['bld', '건물']].map(([k, l]) =>
          <button key={k} className={show[k] ? 'primary' : ''} onClick={() => tog(k)}>{l}</button>)}
        <button className={rotate ? 'primary' : ''} onClick={() => setRotate(!rotate)}>자동 회전</button>
        <button onClick={onClose}>닫기 (Esc)</button>
      </div>
      <div className="overlay" style={{ left: 12, bottom: 40, width: 300, padding: '10px 12px', fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.55 }}>
        <div style={{ color: 'var(--text)' }}>등농도면 (공기 중 0~50 m)</div>
        {d?.iso?.map((s: any, k: number) => { const c = ppmColor(s.ppm); return <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ width: 22, height: 10, borderRadius: 2, background: `rgba(${c[0]},${c[1]},${c[2]},${0.35 + 0.25 * k})` }} />{fmtPpm(s.ppm)} ppm 이상</div> })}
        <div style={{ color: 'var(--text)', marginTop: 6 }}>지면 색 = 지상 2 m 농도 (ppm)</div>
        <div style={{ height: 9, borderRadius: 3, background: legendGradient(2.2), marginTop: 3 }} />
        <div style={{ position: 'relative', height: 16, fontSize: 12 }} className="mono">
          {LEGEND_TICKS_MAIN.map((t) => <span key={t.v} style={{ position: 'absolute', left: `${t.pos}%`, transform: 'translateX(-50%)' }}>{t.v}</span>)}</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '2px 10px' }}>
          {[['home', '주택'], ['exposed', '주택(0.6 ppm↑)'], ['barn', '축사'], ['school', '학교·노유자']].map(([k, l]) => <span key={k}><span className="dot" style={{ background: BLD_COL[k], borderRadius: 2 }} />{l}</span>)}
          <span><span className="dot" style={{ background: '#f43f5e' }} />배출원</span>
        </div>
        {show.ratio
          ? <div style={{ marginTop: 4 }}>바람 세기: <span style={{ color: '#60a5fa' }}>느려짐 0.7×</span> ─ 1× ─ <span style={{ color: '#fb923c' }}>1.3× 빨라짐</span><div className="muted" style={{ fontSize: 12 }}>같은 높이 중앙값 대비 · 지형·건물 영향</div></div>
          : <div style={{ marginTop: 4 }}><span style={{ color: '#7dd3fc' }}>─</span> 바람 유선 (높이 10·30 m)</div>}
        <div style={{ marginTop: 2 }}><span style={{ color: '#7dd3fc' }}>➤</span> 바람 방향 · <span style={{ color: '#e2e8f0' }}>↑</span> 북쪽 (흰 화살표)</div>
      </div>
      {d && <div className="provenance" title="">이 농장 배출원만 계산 · 2 km × 2 km · 배출량 {d.q_total.toFixed(3)} g/s (가정) · 풍향 변동 σθ {d.sigma ?? 3}° · 중립·정상 상태 · 높이 ×{EXAG} 과장 · U-FNO+F 바람 → FNO 농도 (CFD 학습)</div>}
    </div>
  )
}

function Chip({ l, v, sub, color }: { l: string; v: string; sub?: string; color?: string }) {
  return <div style={{ background: 'rgba(13,20,32,.88)', border: '1px solid var(--line-2)', borderRadius: 8, padding: '5px 11px' }}>
    <div style={{ fontSize: 12, color: '#9fb2cc' }}>{l}</div><div className="mono" style={{ fontSize: 16, color: color ?? '#e2e8f0' }}>{v}</div>
    {sub && <div style={{ fontSize: 11, color: '#6f84a3' }}>{sub}</div>}</div>
}

/** 형상만 해제 (재질은 컴포넌트가 끝날 때 한 번에 해제) */
function disposeGeometries(root: THREE.Object3D) { root.traverse((o: any) => o.geometry?.dispose?.()) }

/** 레이어 표시 · 바람 세기 색 */
function applyView(c: any, show: Record<string, boolean>) {
  c.world.children.forEach((o: any) => {
    if (o.userData.layer === 'wind') o.visible = show.wind
    else if (o.userData.layer) o.visible = show[o.userData.layer] ?? true
    if (o.userData.keepShape) o.scale.z = 1 / c.exag              // 구·표지는 찌그러지지 않게
  })
  const f = c.flow
  if (f?.lg) {
    const want = show.ratio ? 'ratio' : 'base'
    if (f.cur !== want) { const at = f.lg.getAttribute('color') as THREE.BufferAttribute; (at.array as Float32Array).set(want === 'ratio' ? f.ratio : f.base); at.needsUpdate = true; f.cur = want }
    ;(c.mats.lines as THREE.LineBasicMaterial).opacity = show.ratio ? 0.65 : 0.26
  }
  if (c.lastArrow) c.arrow.position.set(c.lastArrow.x, c.lastArrow.y, c.lastArrow.z * c.exag)
  c.north.position.set(-1080, -1080, 5 * c.exag)
}

function buildScene(c: any, d: any, tex: THREE.Texture) {
  const world: THREE.Group = c.world, mats = c.mats
  // 지형: 101×101 정점(20 m 간격), 북쪽 행부터 (PlaneGeometry 정점 순서와 같음)
  const n = d.terrain.n, size = d.terrain.size
  const tg = new THREE.PlaneGeometry(size, size, n - 1, n - 1)
  const pos = tg.attributes.position as THREE.BufferAttribute
  for (let i = 0; i < pos.count; i++) pos.setZ(i, d.terrain.z[i])
  tg.computeVertexNormals()
  const tm = mats.terrain as THREE.MeshStandardMaterial
  const first = !tm.map; const old = c.tex; tm.map = tex; c.tex = tex
  if (first) tm.needsUpdate = true
  old?.dispose()
  world.add(new THREE.Mesh(tg, tm))

  // 건물: 용도별로 합쳐 한 번에 그린다
  const groups: Record<string, THREE.BufferGeometry[]> = {}
  for (const b of d.buildings) {
    const p = b.p; if (p.length < 6) continue
    const sh = new THREE.Shape(); sh.moveTo(p[0], p[1]); for (let i = 2; i < p.length; i += 2) sh.lineTo(p[i], p[i + 1])
    const g = new THREE.ExtrudeGeometry(sh, { depth: Math.max(2, b.h), bevelEnabled: false }); g.translate(0, 0, b.z - 0.5)
    const k = b.k === 'home' && b.c >= 0.6 ? 'exposed' : b.k
    ;(groups[k] ||= []).push(g)
  }
  for (const [k, gs] of Object.entries(groups)) {
    const merged = mergeGeometries(gs, false); gs.forEach((g) => g.dispose())
    if (!merged) continue
    const m = new THREE.Mesh(merged, mats[`bld:${k}`] || mats['bld:other']); m.userData.layer = 'bld'; world.add(m)
  }

  // 등농도면 (낮은 농도부터, 반투명). 색은 재질 속성만 바꾼다.
  d.iso.forEach((s: any, k: number) => {
    const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.Float32BufferAttribute(s.v, 3)); g.setIndex(s.f); g.computeVertexNormals()
    const col = ppmColor(s.ppm); const mat = mats[`iso:${Math.min(k, 2)}`] as THREE.MeshPhongMaterial
    mat.color.setStyle(`rgb(${col[0] | 0},${col[1] | 0},${col[2] | 0})`); mat.emissive.setStyle(`rgb(${(col[0] * 0.25) | 0},${(col[1] * 0.25) | 0},${(col[2] * 0.25) | 0})`)
    const mesh = new THREE.Mesh(g, mat); mesh.renderOrder = 10 + k; mesh.userData.layer = 'iso'; world.add(mesh)
  })

  // 바람 유선 + 흐르는 입자. 색은 두 벌: 기본(높이별) / 바람 세기(같은 높이 중앙값 대비 비율)
  const med = (a: number[]) => { if (!a.length) return 1; const s = [...a].sort((x, y) => x - y); return s[s.length >> 1] || 1 }
  const lo: number[] = [], hi: number[] = []
  for (const L of d.streamlines) for (const v of L.s) (L.agl < 20 ? lo : hi).push(v)
  const ref = { lo: med(lo), hi: med(hi) }
  const sp: number[] = [], base: number[] = [], ratio: number[] = []; const lines: { p: Float32Array; s: Float32Array }[] = []
  for (const L of d.streamlines) {
    const p = L.p, s = L.s
    if (s.length < 2) continue
    const r0 = L.agl < 20 ? ref.lo : ref.hi
    for (let i = 0; i + 1 < s.length; i++) {
      sp.push(p[3 * i], p[3 * i + 1], p[3 * i + 2], p[3 * i + 3], p[3 * i + 4], p[3 * i + 5])
      base.push(...windRGB(s[i], L.agl), ...windRGB(s[i + 1], L.agl))
      ratio.push(...ratioRGB(s[i] / r0), ...ratioRGB(s[i + 1] / r0))
    }
    lines.push({ p: new Float32Array(p), s: new Float32Array(s) })
  }
  const lg = new THREE.BufferGeometry(); lg.setAttribute('position', new THREE.Float32BufferAttribute(sp, 3)); lg.setAttribute('color', new THREE.Float32BufferAttribute(base, 3))   // 색 버퍼 하나를 두고 내용만 바꾼다
  const lmesh = new THREE.LineSegments(lg, mats.lines); lmesh.userData.layer = 'wind'; world.add(lmesh)
  const NP = Math.min(700, lines.length * 3)
  const pp = new Float32Array(NP * 3); const state = Array.from({ length: NP }, (_, i) => ({ l: i % Math.max(1, lines.length), t: Math.random() * 10 }))
  const pg = new THREE.BufferGeometry(); pg.setAttribute('position', new THREE.BufferAttribute(pp, 3))
  const pts = new THREE.Points(pg, mats.points); pts.userData.layer = 'wind'; pts.frustumCulled = false; world.add(pts)
  c.flow = { lines, state, pp, pg, lg, base: new Float32Array(base), ratio: new Float32Array(ratio), cur: 'base' }

  // 배출원 표시 (구 + 수직 표지)
  for (const m of d.members) {
    const s = new THREE.Mesh(new THREE.SphereGeometry(14, 20, 14), mats.src); s.position.set(m.x, m.y, m.z); s.userData.keepShape = true; world.add(s)
    const beam = new THREE.Mesh(new THREE.CylinderGeometry(2.5, 2.5, 140, 8), mats.beam)
    beam.rotation.x = Math.PI / 2; beam.position.set(m.x, m.y, m.z + 70); world.add(beam)
  }

  // 풍향 화살표 (바람이 불어 가는 방향, 바람이 들어오는 쪽 가장자리 위). 화살표는 world 밖이라 높이만 직접 맞춘다.
  const dir = new THREE.Vector3(d.flow[0], d.flow[1], 0).normalize()
  c.arrow.setDirection(dir); c.lastArrow = { x: -dir.x * 950, y: -dir.y * 950, z: d.terrain.zmax + 70 }
  // 회전 중심: 농장이 바뀔 때만 지면 높이로 (20초 갱신 때 시점이 튀지 않게)
  if (c.code !== d.code) {
    c.code = d.code; c.groundZ = d.ground_z ?? 0
    c.controls.target.set(c.cam?.tx ?? 0, c.cam?.ty ?? 0, c.groundZ * c.exag)
    if (c.cam) {                                         // 지정 시점: 방위(북 0°, 시계 방향)에서 바라봄
      const az = (c.cam.az * Math.PI) / 180, el = ((c.cam.el ?? 29) * Math.PI) / 180, r = c.cam.dist ?? 3130
      c.camera.position.set((c.cam.tx ?? 0) + r * Math.cos(el) * Math.sin(az), (c.cam.ty ?? 0) + r * Math.cos(el) * Math.cos(az), r * Math.sin(el) + c.groundZ * c.exag)
    } else c.camera.position.set(-1500, -2300, 1500 + c.groundZ * c.exag)
  }
}

function windRGB(s: number, agl: number) {
  const t = Math.min(1, s / 7)
  const lo = agl < 20 ? [0.22, 0.74, 0.97] : [0.65, 0.55, 0.98]       // 10 m 하늘색 / 30 m 보라
  return [lo[0] + (1 - lo[0]) * t * 0.7, lo[1] + (1 - lo[1]) * t * 0.7, lo[2] + (1 - lo[2]) * t * 0.7]
}
/** 풍속비 → 파랑(0.7× 이하) · 흰색(1×) · 주황(1.3× 이상) */
function ratioRGB(r: number) {
  const B = [0.23, 0.51, 0.96], W = [0.9, 0.93, 0.97], O = [0.98, 0.57, 0.24]
  if (r <= 1) { const t = Math.max(0, Math.min(1, (1 - r) / 0.3)); return W.map((w, i) => w + (B[i] - w) * t) }
  const t = Math.max(0, Math.min(1, (r - 1) / 0.3)); return W.map((w, i) => w + (O[i] - w) * t)
}

/** 입자를 유선을 따라 이동 (속도에 비례) */
function stepParticles(c: any) {
  const f = c?.flow; const dt = Math.min(0.05, c?.clock?.getDelta?.() ?? 0)
  if (!f || !f.lines.length) return
  for (let i = 0; i < f.state.length; i++) {
    const st = f.state[i]; let L = f.lines[st.l]; let n = L.s.length
    if (st.t >= n - 1) { st.l = (Math.random() * f.lines.length) | 0; st.t = 0; L = f.lines[st.l]; n = L.s.length }
    const k0 = Math.min(Math.floor(st.t), n - 2)
    st.t += dt * (0.6 + L.s[k0] * 0.9)
    const k = Math.min(Math.floor(st.t), n - 2); const a = Math.min(1, st.t - k)
    const p = L.p
    f.pp[3 * i] = p[3 * k] + (p[3 * k + 3] - p[3 * k]) * a
    f.pp[3 * i + 1] = p[3 * k + 1] + (p[3 * k + 4] - p[3 * k + 1]) * a
    f.pp[3 * i + 2] = p[3 * k + 2] + (p[3 * k + 5] - p[3 * k + 2]) * a + 1.5
  }
  f.pg.attributes.position.needsUpdate = true
}
