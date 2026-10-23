import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev: http://localhost:3000 with /api proxied to the FastAPI backend on :8000.
// Build: frontend/build, served by FastAPI at http://localhost:8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    strictPort: true,
    proxy: { '/api': 'http://localhost:8000', '/health': 'http://localhost:8000' },
  },
  build: { outDir: 'build', emptyOutDir: true },
});
