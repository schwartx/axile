import { resolve } from 'node:path'
import { mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { createHash } from 'node:crypto'
const root = resolve(import.meta.dir, '../..')
const target = process.env.REPLAY_HEAT_TARGET_DIR ?? resolve(tmpdir(), `axile-replay-heat-${createHash('sha256').update(root).digest('hex').slice(0, 12)}`)
const run = (cmd: string[]) => {
  const result = Bun.spawnSync(cmd, { cwd: root, env: { ...process.env, CARGO_TARGET_DIR: target, CARGO_INCREMENTAL: '0', RUSTC_WRAPPER: '' }, stdout: 'inherit', stderr: 'inherit' })
  if (result.exitCode) process.exit(result.exitCode)
}
const version = Bun.spawnSync(['wasm-bindgen', '--version']).stdout.toString().trim()
if (version !== 'wasm-bindgen 0.2.128') throw Error('Install wasm-bindgen-cli 0.2.128 and rustup target add wasm32-unknown-unknown')
run(['cargo', 'build', '--manifest-path', 'rust/replay-heat/Cargo.toml', '--target', 'wasm32-unknown-unknown', '--release', '--locked'])
const output = resolve(root, 'ui/src/features/replay/heat-wasm')
mkdirSync(output, { recursive: true })
run(['wasm-bindgen', resolve(target, 'wasm32-unknown-unknown/release/axile_replay_heat.wasm'), '--target', 'web', '--out-dir', output])
