import { statSync } from 'node:fs'
import { resolve } from 'node:path'
import type { Plugin } from 'vite'

const groups = {
  analysis: ['replay/heat-wasm/axile_replay_heat_bg.wasm'],
  raw: ['replay/raw-wasm/axile_replay_heat_bg.wasm'],
  editor: ['editor/wasm/ty_wasm_bg.wasm.bin', 'editor/wasm/library.json.bin'],
}

/** URLs and sizes come from the same generated files in dev and production. */
export function wasmAssets(): Plugin {
  return {
    name: 'axile-wasm-assets',
    resolveId(id) { if (id === 'virtual:wasm-assets') return '\0virtual:wasm-assets' },
    load(id) {
      if (id !== '\0virtual:wasm-assets') return
      const imports: string[] = []
      const entries = Object.entries(groups).map(([kind, files]) => {
        const assets = files.map(file => {
          const path = resolve(import.meta.dirname, '../src/features', file)
          const name = `asset${imports.length}`
          imports.push(`import ${name} from ${JSON.stringify(path + '?url')};`)
          return `{url:${name},bytes:${statSync(path).size}}`
        })
        return `${kind}:[${assets.join(',')}]`
      })
      return `${imports.join('\n')}\nexport default {${entries.join(',')}};`
    },
  }
}
