import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// GitHub Pages serves project sites from /<repo-name>/, so the built asset
// URLs need that prefix. Override with VITE_BASE for a custom domain ("/").
export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE || '/SeeQ-Flare-Project/',
})
