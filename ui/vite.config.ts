import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// @types/node is not installed; only process.env is needed here.
declare const process: { env: Record<string, string | undefined> };

// The Python core serves the built UI from src/jarvis/ui/static at "/".
// In `npm run dev`, /api and /ws are proxied to a locally running core (default port 8765);
// open http://localhost:5173/?token=<token> (dev only) or /?mock=1 for the simulator.
const CORE = process.env.JARVIS_CORE_URL ?? 'http://127.0.0.1:8765';

export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: '../src/jarvis/ui/static',
    emptyOutDir: true,
    target: 'es2022',
    sourcemap: false,
    assetsInlineLimit: 2048,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: {
          three: ['three'],
          react: ['react', 'react-dom'],
        },
      },
    },
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': { target: CORE, changeOrigin: false },
      '/ws': { target: CORE.replace(/^http/, 'ws'), ws: true },
    },
  },
});
