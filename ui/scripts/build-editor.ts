import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync, copyFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'

const root = resolve(import.meta.dir, '../..')
const revision = '36b92bb23ebca423672c37501299e06b836a060e'
const archiveHash = '61a0a5b597b8174f7e5c15ffd80e586bdb9f33b1dfa88dd23f14a68b5595cec1'
const hash = (value: string | Uint8Array) => createHash('sha256').update(value).digest('hex')
const bridge = readFileSync(resolve(root, 'rust/editor-wasm/axile.rs'))
const lock = readFileSync(resolve(root, 'rust/editor-wasm/Cargo.lock'))
const fingerprint = hash(Buffer.concat([bridge, lock, Buffer.from(revision), readFileSync(import.meta.path)]))
const cache = resolve(tmpdir(), `axile-editor-${hash(root).slice(0, 12)}`)
const source = resolve(cache, 'source')
const target = resolve(cache, 'target')
const output = resolve(root, 'ui/src/features/editor/wasm')
const run = (cmd: string[], cwd = root) => {
  const result = Bun.spawnSync(cmd, { cwd, env: { ...process.env, CARGO_TARGET_DIR: target,
    CARGO_INCREMENTAL: '0', RUSTC_WRAPPER: '', TY_WASM_COMMIT_SHORT_HASH: revision }, stdout: 'inherit', stderr: 'inherit' })
  if (result.exitCode) throw new Error(`${cmd[0]} failed: ${result.exitCode}`)
}

const version = Bun.spawnSync(['wasm-bindgen', '--version']).stdout.toString().trim()
if (version !== 'wasm-bindgen 0.2.130') throw new Error('Install wasm-bindgen-cli 0.2.130 and the wasm32-unknown-unknown target')
mkdirSync(output, { recursive: true })
const stamp = resolve(output, '.fingerprint')
if (!existsSync(stamp) || readFileSync(stamp, 'utf8') !== fingerprint || !existsSync(resolve(output, 'ty_wasm_bg.wasm'))) {
  mkdirSync(cache, { recursive: true })
  if (!existsSync(resolve(cache, 'upstream.tar.gz'))) {
    const response = await fetch(`https://codeload.github.com/astral-sh/ruff/tar.gz/${revision}`)
    if (!response.ok) throw new Error(`ty source download failed: ${response.status}`)
    const archive = new Uint8Array(await response.arrayBuffer())
    if (hash(archive) !== archiveHash) throw new Error('ty source archive checksum mismatch')
    writeFileSync(resolve(cache, 'upstream.tar.gz'), archive)
  }
  if (hash(readFileSync(resolve(cache, 'upstream.tar.gz'))) !== archiveHash) throw new Error('Cached ty source checksum mismatch')
  mkdirSync(source, { recursive: true })
  run(['tar', '-xzf', resolve(cache, 'upstream.tar.gz'), '--strip-components=1', '-C', source])
  const manifest = resolve(source, 'Cargo.toml')
  writeFileSync(manifest, readFileSync(manifest, 'utf8').replace('members = ["crates/*"]', 'members = ["crates/ty_wasm"]')
    .replace('wasm-bindgen = { version = "0.2.92"', 'wasm-bindgen = { version = "=0.2.130"'))
  const crate = resolve(source, 'crates/ty_wasm')
  writeFileSync(resolve(crate, 'Cargo.toml'), readFileSync(resolve(crate, 'Cargo.toml'), 'utf8')
    .replace('[dependencies]', '[dependencies]\nserde_json = { workspace = true }\nruff_python_ast = { workspace = true }'))
  writeFileSync(resolve(crate, 'src/lib.rs'), readFileSync(resolve(crate, 'src/lib.rs'), 'utf8') + '\nmod axile;\n')
  writeFileSync(resolve(crate, 'src/axile.rs'), bridge)
  writeFileSync(resolve(source, 'Cargo.lock'), lock)
  run(['cargo', 'build', '--manifest-path', manifest, '-p', 'ty_wasm', '--target', 'wasm32-unknown-unknown', '--release', '--locked'])
  run(['wasm-bindgen', resolve(target, 'wasm32-unknown-unknown/release/ty_wasm.wasm'), '--target', 'web', '--out-dir', output])
  copyFileSync(resolve(source, 'LICENSE'), resolve(output, 'LICENSE'))
  writeFileSync(stamp, fingerprint)
}
run(['uv', 'run', '--locked', '--project', root, 'python', resolve(root, 'scripts/build_editor_library.py'), output])
// Axile always provides the decompressed response; omit the unused 31 MB default asset.
const bindings = resolve(output, 'ty_wasm.js')
writeFileSync(bindings, readFileSync(bindings, 'utf8').replace("module_or_path = new URL('ty_wasm_bg.wasm', import.meta.url);", "throw new Error('Pass the editor WASM response explicitly');"))
