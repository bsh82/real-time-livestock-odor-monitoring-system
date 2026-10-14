import { useNavigate, useParams } from 'react-router-dom'
import Farm3D from './Farm3D'

/** 3D 확산 단독 화면 (/farm3d/:code) — 모니터 2 나 별도 창에 띄울 때 */
export default function Farm3DPage() {
  const { code } = useParams(); const nav = useNavigate()
  return <div style={{ position: 'fixed', inset: 0 }}><Farm3D code={code || ''} onClose={() => nav('/ops')} /></div>
}
