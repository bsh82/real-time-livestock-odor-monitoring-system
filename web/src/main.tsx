import React, { useEffect } from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import '@fontsource/noto-sans-kr/400.css'
import '@fontsource/noto-sans-kr/500.css'
import '@fontsource/noto-sans-kr/700.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/600.css'
import './styles.css'
import { boot } from './store'
import OpsPage from './OpsPage'
import InsightPage from './InsightPage'
import Farm3DPage from './Farm3DPage'

function App() {
  useEffect(() => { boot() }, [])
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/ops" element={<OpsPage />} />
        <Route path="/insight" element={<InsightPage />} />
        <Route path="/farm3d/:code" element={<Farm3DPage />} />
        <Route path="*" element={<Navigate to="/ops" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(<App />)
