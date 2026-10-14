import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 개발 중에는 API(:8000)·필드 이미지·WebSocket 을 프록시한다. 빌드 결과(dist)는 API 서버가 그대로 서빙한다.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/fields': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
  build: { chunkSizeWarningLimit: 3000 },
})
