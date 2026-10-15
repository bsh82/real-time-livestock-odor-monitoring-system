import { useEffect, useMemo, useState } from 'react'
import dayjs from 'dayjs'
import ReactECharts from 'echarts-for-react'
import { KIND_KO, LEVEL_KO, LEVEL_COLOR } from './store'

/** 레이크하우스 gold 표 읽기 (/api/stats → Trino → Iceberg). 서버가 30초 캐시 */
export function useStat(name: string, every = 30000) {
  const [d, setD] = useState<any[]>([])
  useEffect(() => { const load = () => fetch(`/api/stats/${name}`).then((r) => r.json()).then((x) => Array.isArray(x) && setD(x)).catch(() => {}); load(); const t = setInterval(load, every); return () => clearInterval(t) }, [name])
  return d
}

const AX = { axisLine: { lineStyle: { color: '#2a3b57' } }, axisLabel: { fontSize: 11, color: '#9fb2cc' }, splitLine: { lineStyle: { color: '#16233a' } } }
const NAME = { nameTextStyle: { color: '#6f84a3', fontSize: 11 } }
const base = (extra: any) => ({
  backgroundColor: 'transparent', textStyle: { color: '#9fb2cc', fontFamily: 'Noto Sans KR' }, animation: false,
  tooltip: { trigger: 'axis', backgroundColor: '#0d1420', borderColor: '#2a3b57', textStyle: { color: '#dbe6f5', fontSize: 12 } },
  legend: { left: 'center', top: 0, itemWidth: 14, itemHeight: 8, itemGap: 14, textStyle: { color: '#9fb2cc', fontSize: 11.5 } },
  grid: { left: 50, right: 50, top: 52, bottom: 26 }, ...extra,
})
const num = (v: any) => (v == null ? '-' : Number(v).toLocaleString())

/** 통계 화면의 '골드 통계' 보기: gold 표 7개를 차트로 */
export default function GoldStats() {
  const monthly = useStat('monthly'); const hourly = useStat('hourly_profile'); const rank = useStat('farm_rank')
  const expo = useStat('cluster_exposure'); const alarm = useStat('alarm_summary'); const daily = useStat('daily_stream')
  const comp = useStat('complaint_summary'); const tables = useStat('tables', 15000)
  const [lake, setLake] = useState<any>(null)
  useEffect(() => { const load = () => fetch('/api/lake/status').then((r) => r.json()).then(setLake).catch(() => {}); load(); const t = setInterval(load, 15000); return () => clearInterval(t) }, [])

  const gold = tables.filter((t) => t.layer === 'gold')
  // 재생 중인 마지막 날: 측정 건수가 다른 날의 중앙값보다 크게 적으면 '진행 중'
  const fullDay = (() => { const r = daily.slice(0, -1).map((d) => Number(d.readings) || 0).sort((a, b) => a - b); return r.length ? r[Math.floor(r.length / 2)] : 0 })()
  const partial = daily.length > 1 && (Number(daily[daily.length - 1].readings) || 0) < 0.9 * fullDay
  const isPart = (i: number) => partial && i === daily.length - 1
  const snap = lake?.snapshots?.[0]
  const nComp = comp.reduce((a, c) => a + (Number(c.n) || 0), 0)
  const kpis: [string, string, string][] = [
    ['gold 표', `${gold.length}개`, `${num(gold.reduce((a, t) => a + (Number(t.row_count) || 0), 0))}행 · Iceberg`],
    ['최근 갱신', snap ? dayjs(snap.committed_at).format('HH:mm:ss') : '-', `스냅샷 ${num(lake?.snapshot_count ?? lake?.snapshots?.length)}개 (시간 여행)`],
    ['배치', lake?.airflow?.alive ? 'Airflow' : '예비 실행기', lake?.airflow?.alive ? '5분 주기 DAG' : '120초 주기'],
    ['2년 기록', monthly.length ? `${monthly.length}개월` : '-', monthly.length ? `${dayjs(monthly[0].month).format('YYYY-MM')} ~ ${dayjs(monthly[monthly.length - 1].month).format('YYYY-MM')}` : ''],
    ['재생 구간', daily.length ? `${daily.length - (partial ? 1 : 0)}일${partial ? ' + 진행 중' : ''}` : '-', `측정 ${num(daily.reduce((a, d) => a + (Number(d.readings) || 0), 0))}건`],
    ['민원', `${nComp}건`, comp[0] ? `주 원인 ${comp[0].top_farm}` : '-'],
  ]

  const optMonthly = useMemo(() => base({
    xAxis: { type: 'category', data: monthly.map((m) => dayjs(m.month).format('YY-MM')), ...AX, splitLine: { show: false } },
    yAxis: [{ type: 'value', name: 'ppm', min: 0, ...AX, ...NAME }, { type: 'value', name: '%', min: 0, ...AX, ...NAME, splitLine: { show: false } }],
    series: [
      { name: '5 ppm 초과 비율 (센서·시간)', type: 'bar', yAxisIndex: 1, itemStyle: { color: '#1e3a5f' }, barWidth: '60%',
        data: monthly.map((m) => (m.data_hours ? Math.round((1000 * m.hours_over_5ppm) / m.data_hours) / 10 : null)) },
      { name: '평균', type: 'line', data: monthly.map((m) => m.nh3_avg), smooth: true, showSymbol: false, lineStyle: { color: '#38bdf8', width: 2 }, itemStyle: { color: '#38bdf8' } },
      { name: '상위 5%', type: 'line', data: monthly.map((m) => m.nh3_p95), smooth: true, showSymbol: false, lineStyle: { color: '#f97316', width: 2 }, itemStyle: { color: '#f97316' } },
    ],
  }), [monthly])

  const optHourly = useMemo(() => base({
    xAxis: { type: 'category', data: hourly.map((h) => `${h.hour}시`), ...AX, splitLine: { show: false }, axisLabel: { ...AX.axisLabel, interval: 2 } },
    yAxis: [{ type: 'value', name: 'NH₃ ppm', min: 0, ...AX, ...NAME }, { type: 'value', name: '℃', scale: true, ...AX, ...NAME, splitLine: { show: false } }],
    series: [
      { name: '평균 NH₃', type: 'bar', data: hourly.map((h) => h.nh3_avg), itemStyle: { color: '#0e7490' }, barWidth: '62%' },
      { name: '평균 기온', type: 'line', yAxisIndex: 1, data: hourly.map((h) => h.temp_avg), smooth: true, showSymbol: false, lineStyle: { color: '#fbbf24', width: 2 }, itemStyle: { color: '#fbbf24' } },
    ],
  }), [hourly])

  const top = rank.slice(0, 10).reverse()
  const optRank = useMemo(() => base({
    grid: { left: 150, right: 54, top: 10, bottom: 26 }, legend: { show: false },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: '#0d1420', borderColor: '#2a3b57', textStyle: { color: '#dbe6f5', fontSize: 12 },
      formatter: (p: any) => { const r = top[p[0].dataIndex]; return `${r.rank}위 ${r.farm}<br/>평균 ${r.nh3_avg} ppm · 최대 ${r.nh3_max} ppm<br/>5 ppm 초과 ${r.data_hours ? Math.round((100 * r.hours_over_5ppm) / r.data_hours) : '-'}% (센서·시간 ${num(r.hours_over_5ppm)} / ${num(r.data_hours)})` } },
    xAxis: { type: 'value', name: 'ppm', min: 0, ...AX, ...NAME },
    yAxis: { type: 'category', data: top.map((r) => `${r.rank}. ${r.farm.length > 9 ? r.farm.slice(0, 9) + '…' : r.farm}`), ...AX, splitLine: { show: false }, axisLabel: { ...AX.axisLabel, fontSize: 11.5, color: '#dbe6f5' } },
    series: [{ type: 'bar', data: top.map((r) => r.nh3_avg), barWidth: '62%',
      itemStyle: { color: (p: any) => (top[p.dataIndex].nh3_avg >= 10 ? '#ef4444' : top[p.dataIndex].nh3_avg >= 5 ? '#f59e0b' : '#38bdf8') },
      label: { show: true, position: 'right', fontSize: 11, color: '#9fb2cc', formatter: (p: any) => Number(p.value).toFixed(1) } }],
  }), [rank])

  const ex = [...expo].sort((a, b) => b.exposure_hours - a.exposure_hours || b.cmax_ppm - a.cmax_ppm || String(a.cluster).localeCompare(String(b.cluster))).slice(0, 10)
  const optExpo = useMemo(() => base({
    xAxis: { type: 'category', data: ex.map((c) => c.cluster), ...AX, splitLine: { show: false } },
    yAxis: [{ type: 'value', name: '노출 시간', min: 0, ...AX, ...NAME }, { type: 'value', name: '최대 ppm', min: 0, ...AX, ...NAME, splitLine: { show: false } }],
    tooltip: { trigger: 'axis', backgroundColor: '#0d1420', borderColor: '#2a3b57', textStyle: { color: '#dbe6f5', fontSize: 12 },
      formatter: (p: any) => { const c = ex[p[0].dataIndex]; return `${c.cluster} 권역<br/>주택 0.6 ppm↑ ${c.exposure_hours}시간 · 최대 ${c.max_homes}동<br/>최고 예측 ${c.cmax_ppm} ppm · 평균 배출 ${c.q_avg_gs} g/s` } },
    series: [
      { name: '주택 노출 시간 (0.6 ppm↑)', type: 'bar', data: ex.map((c) => c.exposure_hours), itemStyle: { color: '#d946ef' }, barWidth: '55%' },
      { name: '최고 예측 농도', type: 'scatter', yAxisIndex: 1, data: ex.map((c) => c.cmax_ppm), symbol: 'diamond', symbolSize: 11, itemStyle: { color: '#fde68a' } },
    ],
  }), [expo])

  const kinds = [...new Set(alarm.map((a) => a.kind))]
  const levels = ['warning', 'caution', 'fault'].filter((l) => alarm.some((a) => a.level === l))
  const optAlarm = useMemo(() => base({
    grid: { left: 100, right: 30, top: 40, bottom: 26 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: '#0d1420', borderColor: '#2a3b57', textStyle: { color: '#dbe6f5', fontSize: 12 },
      formatter: (p: any) => { const k = kinds[p[0].dataIndex]; const rs = alarm.filter((a) => a.kind === k)
        return `${KIND_KO[k] || k}<br/>` + rs.map((a) => `${LEVEL_KO[a.level] || a.level} ${a.n}건 · 해제 ${a.cleared} · 조치 ${a.actions}`).join('<br/>') } },
    xAxis: { type: 'value', name: '건', min: 0, ...AX, ...NAME },
    yAxis: { type: 'category', data: kinds.map((k) => KIND_KO[k] || k), ...AX, splitLine: { show: false }, axisLabel: { ...AX.axisLabel, fontSize: 12, color: '#dbe6f5' } },
    series: levels.map((l) => ({ name: LEVEL_KO[l] || l, type: 'bar', stack: 'n', barWidth: '55%', itemStyle: { color: LEVEL_COLOR[l] ? `rgb(${LEVEL_COLOR[l].join(',')})` : '#64748b' },
      data: kinds.map((k) => alarm.filter((a) => a.kind === k && a.level === l).reduce((s, a) => s + (Number(a.n) || 0), 0)) })),
  }), [alarm])

  const optDaily = useMemo(() => base({
    grid: { left: 50, right: 50, top: 52, bottom: 40 },
    xAxis: { type: 'category', data: daily.map((d, i) => dayjs(d.day).format('MM-DD') + (isPart(i) ? '\n진행 중' : '')), ...AX, splitLine: { show: false } },
    yAxis: [{ type: 'value', name: 'kg/일', min: 0, ...AX, ...NAME }, { type: 'value', name: 'ppm', min: 0, ...AX, ...NAME, splitLine: { show: false } }],
    tooltip: { trigger: 'axis', backgroundColor: '#0d1420', borderColor: '#2a3b57', textStyle: { color: '#dbe6f5', fontSize: 12 },
      formatter: (p: any) => { const d = daily[p[0].dataIndex]; return `${dayjs(d.day).format('YYYY-MM-DD')}${isPart(p[0].dataIndex) ? ' (진행 중 — 하루가 아직 끝나지 않음)' : ''}<br/>추정 배출 ${d.emission_kg} kg · 평균 ${d.nh3_avg} ppm<br/>측정 ${num(d.readings)}건 · 배출원 ${d.sources}곳` } },
    series: [
      { name: '추정 NH₃ 배출량', type: 'bar', barWidth: '55%', itemStyle: { color: '#15803d' },
        data: daily.map((d, i) => (isPart(i) ? { value: d.emission_kg, itemStyle: { color: '#15803d', opacity: 0.35, borderColor: '#4ade80', borderType: 'dashed', borderWidth: 1 } } : d.emission_kg)) },
      { name: '평균 농도', type: 'line', yAxisIndex: 1, data: daily.map((d) => d.nh3_avg), smooth: true, symbol: 'circle', symbolSize: 5, lineStyle: { color: '#38bdf8', width: 2 }, itemStyle: { color: '#38bdf8' } },
    ],
  }), [daily, partial])

  const charts: [string, string, any, number][] = [
    ['2년 기록 · 월별 NH₃', 'gold.monthly_trend', optMonthly, monthly.length],
    ['시간대별 평균 NH₃ · 기온', 'gold.hourly_profile', optHourly, hourly.length],
    ['농가별 평균 NH₃ 상위 10', 'gold.farm_rank', optRank, rank.length],
    ['권역별 주택 노출 (확산 예측)', 'gold.cluster_exposure', optExpo, expo.length],
    ['경보 유형 · 등급', 'gold.alarm_summary', optAlarm, alarm.length],
    ['재생 구간 일별 배출 · 농도', 'gold.daily_stream', optDaily, daily.length],
  ]
  return (
    <div style={{ display: 'grid', gridTemplateRows: 'auto 1fr', gap: 8, minHeight: 0 }}>
      <div className="goldkpis">{kpis.map(([l, v, s]) => <div key={l} className="gk"><div className="l">{l}</div><div className="v">{v}</div><div className="s">{s}</div></div>)}</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gridTemplateRows: 'repeat(2, 1fr)', gap: 8, minHeight: 0 }}>
        {charts.map(([title, src, opt, n]) => (
          <div key={src} className="panel">
            <h3>{title}<span className="r mono">{src}</span></h3>
            <div style={{ flex: 1, minHeight: 0, padding: '6px 6px 2px' }}>
              {n ? <ReactECharts option={opt} style={{ height: '100%' }} notMerge /> : <div className="muted" style={{ padding: 16, fontSize: 13 }}>아직 표가 없습니다 — 배치가 한 번 돌면 채워집니다.</div>}
            </div>
          </div>))}
      </div>
    </div>
  )
}
