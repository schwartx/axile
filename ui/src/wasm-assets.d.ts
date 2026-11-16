declare module 'virtual:wasm-assets' {
  const assets: Record<'analysis' | 'raw' | 'editor', { url: string; bytes: number }[]>
  export default assets
}
