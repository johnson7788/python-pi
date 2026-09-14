import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Python backend listens on 8787 (see mock-server.mjs, which speaks the same
// protocol). Proxying /api keeps the browser on a single origin in dev, so the
// app can use relative fetch/WebSocket URLs and needs no CORS on the server.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    // ws:true is required because /api/ws is a WebSocket upgrade, not an HTTP call.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
