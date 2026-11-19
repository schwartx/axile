import { useEffect, useSyncExternalStore } from 'react'
import assets from 'virtual:wasm-assets'
import { createWasmDownload } from './wasmDownload'

export type WasmKind = keyof typeof assets
const resources = {
  analysis: createWasmDownload(assets.analysis),
  raw: createWasmDownload(assets.raw),
}
export const wasmResource = (kind: WasmKind) => resources[kind]
export function useWasmResource(kind: WasmKind) {
  const resource = resources[kind]
  const state = useSyncExternalStore(resource.subscribe, resource.getSnapshot, resource.getSnapshot)
  useEffect(() => { void resource.check() }, [resource])
  return { resource, state }
}
