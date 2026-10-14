// 농도 색상표 (로그 연속, 0.0005 ~ 3 ppm). services/engine/viz.py 의 STOPS 와 같은 값 — 바꿀 때 두 곳을 같이 바꿀 것.
export const STOPS: [number, number, number, number, number][] = [
  [0.0005, 30, 64, 160, 34], [0.002, 37, 99, 235, 78], [0.008, 6, 182, 212, 112], [0.03, 34, 197, 94, 140],
  [0.1, 234, 179, 8, 165], [0.3, 249, 115, 22, 190], [0.6, 239, 68, 68, 205], [1.0, 220, 38, 38, 220], [3.0, 192, 38, 211, 235],
]
const L0 = Math.log10(STOPS[0][0]), L1 = Math.log10(STOPS[STOPS.length - 1][0])

/** ppm → [r, g, b, a(0..255)] */
export function ppmColor(ppm: number): number[] {
  const v = Math.log10(Math.max(ppm, 1e-12))
  if (v <= L0) return STOPS[0].slice(1)
  for (let i = 1; i < STOPS.length; i++) {
    const a = Math.log10(STOPS[i - 1][0]), b = Math.log10(STOPS[i][0])
    if (v <= b) { const t = (v - a) / (b - a); return [1, 2, 3, 4].map((k) => STOPS[i - 1][k] + (STOPS[i][k] - STOPS[i - 1][k]) * t) }
  }
  return STOPS[STOPS.length - 1].slice(1)
}

/** 범례용 CSS 그라데이션 (로그 축 위치) */
export function legendGradient(alphaBoost = 1.6) {
  const parts = STOPS.map((s) => `rgba(${s[1]},${s[2]},${s[3]},${Math.min(1, (s[4] / 255) * alphaBoost).toFixed(2)}) ${(((Math.log10(s[0]) - L0) / (L1 - L0)) * 100).toFixed(1)}%`)
  return `linear-gradient(90deg, ${parts.join(', ')})`
}
export const LEGEND_TICKS = [0.0005, 0.002, 0.01, 0.05, 0.2, 0.6, 1, 3].map((v) => ({ v, pos: ((Math.log10(v) - L0) / (L1 - L0)) * 100 }))
/** 화면 범례용 눈금 4개 (10배 간격) */
export const LEGEND_TICKS_MAIN = [0.001, 0.01, 0.1, 1].map((v) => ({ v, pos: ((Math.log10(v) - L0) / (L1 - L0)) * 100 }))
export const fmtPpm = (v: number) => (v >= 1 ? v.toFixed(1) : v >= 0.1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(3) : v >= 0.001 ? v.toFixed(4) : v > 0 ? '0.001 미만' : '0')
/** 단위까지 붙인 농도 표기: 0 → '영향 없음', 0.001 미만 → '0.001 ppm 미만' */
export const fmtPpmU = (v: number | null | undefined) => (v == null ? '-' : v <= 0 ? '영향 없음' : v < 0.001 ? '0.001 ppm 미만' : `${fmtPpm(v)} ppm`)
