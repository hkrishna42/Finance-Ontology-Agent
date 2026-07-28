import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Proxy the API routes to the FastAPI backend so the app uses same-origin relative URLs
// (including the SSE stream) with no CORS in dev. https://vite.dev/config/
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/health': { target: API_TARGET, changeOrigin: true },
      '/ontology': { target: API_TARGET, changeOrigin: true },
      '/ingest': { target: API_TARGET, changeOrigin: true },
      // Panel endpoints (built in parallel by other workers). The data layer falls back to
      // committed fixtures until these are live, then auto-wires with no UI change.
      '/query': { target: API_TARGET, changeOrigin: true },
      '/risk': { target: API_TARGET, changeOrigin: true },
      '/impact': { target: API_TARGET, changeOrigin: true },
      '/reports': { target: API_TARGET, changeOrigin: true },
      '/firms': { target: API_TARGET, changeOrigin: true },
      '/resolve': { target: API_TARGET, changeOrigin: true },
      '/graph': { target: API_TARGET, changeOrigin: true },
      '/documents': { target: API_TARGET, changeOrigin: true },
    },
  },
})
