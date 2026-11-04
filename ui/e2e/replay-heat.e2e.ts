import { expect, test } from '@playwright/test'
test('real Worker and WASM settle after chart drag, wheel and data replacement', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/tests/replay-depth.fixture.html')
  const scale = page.locator('.replay-heat-scale')
  await expect(scale).toContainText('挂量峰值')
  await page.getByRole('button', { name: '验收场景' }).click()
  await page.getByRole('option', { name: '大数据拖动 · 一万盘口与五千成交' }).click()
  await expect(scale).toContainText('挂量峰值')
  const chart = page.getByRole('group', { name: /历史盘口热力图/ })
  const bounds = (await chart.boundingBox())!
  await page.mouse.move(bounds.x + 150, bounds.y + 100)
  await page.mouse.wheel(0, -400)
  await expect(scale).toContainText('挂量峰值')
  await page.evaluate(() => {
    const state = window as Window & { replayFrames?: { values: number[]; active: boolean } }
    const frames = state.replayFrames = { values: [] as number[], active: true }
    let previous = performance.now()
    const sample = (now: number) => { if (!frames.active) return; frames.values.push(now - previous); previous = now; requestAnimationFrame(sample) }
    requestAnimationFrame(sample)
  })
  await page.mouse.down()
  await page.mouse.move(bounds.x + 280, bounds.y + 130, { steps: 16 })
  await expect(scale).toContainText('预览')
  const frameTimes = await page.evaluate(() => {
    const frames = (window as Window & { replayFrames: { values: number[]; active: boolean } }).replayFrames
    frames.active = false
    return frames.values.sort((a,b) => a-b)
  })
  expect(frameTimes.length).toBeGreaterThan(3)
  console.log(JSON.stringify({ scenario: '10000 quotes, 200000 levels, 5000 trades', dragFrames: frameTimes.length, p95FrameMs: frameTimes[Math.floor(frameTimes.length * .95)] }))
  await page.mouse.up()
  await expect(scale).toContainText('挂量峰值')
  await page.getByRole('button', { name: '恢复视图', exact: true }).click()
  await expect(scale).toContainText('挂量峰值')
  await page.getByRole('button', { name: '验收场景' }).click()
  await page.getByRole('option', { name: '买120 / 卖120 · 聚合' }).click()
  await expect(scale).toContainText('挂量峰值')
  await expect(page.getByRole('status')).toContainText('买 120')
  expect(errors).toEqual([])
})

test('measure full worker query round trips against JS on equal data', async ({ page }) => {
  await page.goto('/tests/replay-depth.fixture.html')
  const timings = await page.evaluate(async () => {
    // Vite serves these source modules for the dedicated browser benchmark.
    const { packHeat } = await import('/src/features/replay/heatData.ts')
    const { replayHeatCells, replayChartLayout } = await import('/src/features/replay/chartGeometry.ts')
    const count = 20000, layout = replayChartLayout(1400, 700), view = { min: 99, max: 102, tick: .01 }
    const quotes = Array.from({ length: count }, (_, i) => ({ symbol: 'X', time_us: i * 1000, capture_seq: i, book_valid: true, available: true, valid_until_us: (i + 1) * 1000,
      bids: Array.from({ length: 20 }, (_, j) => ({ price: 100 + j * .01, quantity: (i+j) % 100 })), asks: [],
    }))
    const end = count * 1000, started = performance.now()
    const worker = new Worker('/src/features/replay/heat.worker.ts?worker_file&type=module', { type: 'module' })
    const exchange = (message: unknown, transfer: Transferable[] = []) => new Promise<void>((resolve, reject) => {
      worker.onmessage = event => event.data.kind === 'error' ? reject(Error(event.data.message)) : resolve()
      worker.onerror = event => reject(Error(event.message))
      worker.postMessage(message, transfer)
    })
    try {
      const rows = packHeat([{ quotes, gaps: [], end }])
      await exchange({ kind: 'load', rows }, rows.map((row: Float64Array) => row.buffer))
      const initializationMs = performance.now() - started, js: number[] = [], wasm: number[] = []
      for (let i = 0; i < 8; i++) {
        const range = { start_us: i * end / 100, end_us: end * (.5 + i / 100) }
        let start = performance.now()
        replayHeatCells(quotes, range, end, view, layout, [])
        const jsMs = performance.now() - start
        start = performance.now()
        await exchange({ kind: 'query', id: i, query: { range, view, layout } })
        if (i) { js.push(jsMs); wasm.push(performance.now() - start) }
      }
      const median = (values: number[]) => values.sort((a,b) => a-b)[Math.floor(values.length / 2)]
      return { quotes: count, levels: count * 20, initializationMs, jsMedianMs: median(js), workerRoundTripMedianMs: median(wasm) }
    } finally { worker.terminate() }
  })
  console.log(JSON.stringify(timings))
  expect(timings.workerRoundTripMedianMs).toBeGreaterThan(0)
})

test('heat status and chart height converge around toolbar wrapping widths', async ({ page }) => {
  await page.goto('/tests/replay-depth.fixture.html')
  await page.addStyleTag({ content: '.replay-chart-navigation { min-width: 260px; }' })
  const unstable = await page.evaluate(async () => {
    const chart = document.querySelector<HTMLElement>('.replay-chart')!
    const plot = document.querySelector<HTMLElement>('.replay-chart-plot')!
    const scale = document.querySelector<HTMLElement>('.replay-heat-scale')!
    const failures: unknown[] = []
    for (let width = 600; width <= 1100; width += 25) {
      chart.style.width = `${width}px`
      const heights: number[] = [], states: string[] = []
      for (let frame = 0; frame < 24; frame++) {
        await new Promise(requestAnimationFrame)
        if (frame >= 12) { heights.push(plot.clientHeight); states.push(scale.textContent ?? '') }
      }
      if (new Set(heights).size > 1 || states.some(state => state.includes('计算中'))) failures.push({ width, heights, states })
    }
    return failures
  })
  console.log(JSON.stringify(unstable))
  expect(unstable).toEqual([])
})
