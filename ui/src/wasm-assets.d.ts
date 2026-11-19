declare module 'virtual:wasm-assets' {
  const assets: Record<'analysis' | 'raw', { url: string; bytes: number }[]>
  export default assets
}
