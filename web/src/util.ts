/** 기상학적 풍향(불어오는 방향, °) → 16방위 한글 */
/** 인쇄용 보기: 주소에 ?theme=print → 밝은 바탕 (작품소개서·포스터용 캡처). 전시 화면은 기본 다크 */
export const PRINT = typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('theme') === 'print'

export function dir16(d: number) {
  const n = ['북', '북북동', '북동', '동북동', '동', '동남동', '남동', '남남동', '남', '남남서', '남서', '서남서', '서', '서북서', '북서', '북북서']
  return n[Math.round((((d % 360) + 360) % 360) / 22.5) % 16] + '풍'
}
