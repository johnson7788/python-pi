import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Python backend listens on 8787 (see mock-server.mjs, which speaks the same
// protocol). Proxying /api keeps the browser on a single origin in dev, so the
// app can use relative fetch/WebSocket URLs and needs no CORS on the server.
//
// PI_API_TARGET points that proxy somewhere else — handy when 8787 is already
// taken by a real deployment and you want to develop against `npm run mock`.
//
// Declared here rather than pulled in via @types/node: this project has no other use
// for Node types (nothing under src/ touches Node APIs), and the config file is the
// only place that runs in Node at all.
declare const process: { env: Record<string, string | undefined> }

const apiTarget = process.env.PI_API_TARGET ?? 'http://127.0.0.1:8787'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    // ws:true is required because /api/ws is a WebSocket upgrade, not an HTTP call.
    proxy: {
      '/api': {
        target: apiTarget,
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
