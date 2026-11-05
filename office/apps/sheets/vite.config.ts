import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const pkg = (name: string) => fileURLToPath(new URL(`../../packages/${name}/src`, import.meta.url))
const shim = (name: string) => fileURLToPath(new URL(`./src/renderer/shims/${name}`, import.meta.url))

export default defineConfig({
  root: fileURLToPath(new URL('./src/renderer', import.meta.url)),
  base: '/office/sheets/',
  plugins: [react()],
  resolve: {
    alias: [
      { find: '@genoffice/pptx-engine/background-promote', replacement: `${pkg('pptx-engine')}/background-promote.ts` },
      { find: '@genoffice/pptx-engine/table-grid', replacement: `${pkg('pptx-engine')}/table-grid.ts` },
      { find: '@genoffice/pptx-render/preset-geometry', replacement: `${pkg('pptx-render')}/preset-geometry.ts` },
      { find: '@genoffice/docx-engine/math', replacement: `${pkg('docx-engine')}/math.ts` },
      { find: '@genoffice/docx-engine', replacement: `${pkg('docx-engine')}/index.ts` },
      { find: '@genoffice/pptx-engine', replacement: `${pkg('pptx-engine')}/index.ts` },
      { find: '@genoffice/pptx-render', replacement: `${pkg('pptx-render')}/index.ts` },
      { find: '@genoffice/ai-provider', replacement: `${pkg('ai-provider')}/index.ts` },
      { find: '@genoffice/agent-core', replacement: `${pkg('agent-core')}/index.ts` },
      { find: '@genoffice/project-store', replacement: `${pkg('project-store')}/index.ts` },
      { find: '@genoffice/ui', replacement: `${pkg('ui')}/index.ts` },
      { find: '@genoffice/i18n', replacement: `${pkg('i18n')}/index.ts` },
      { find: 'node:crypto', replacement: shim('node-crypto.ts') },
      { find: 'node:path', replacement: shim('node-path.ts') },
      { find: 'node:os', replacement: shim('node-os.ts') },
      { find: 'node:fs/promises', replacement: shim('node-empty.ts') },
    ],
  },
  build: { outDir: fileURLToPath(new URL('../../../office-dist/sheets', import.meta.url)), emptyOutDir: false, sourcemap: false },
})
