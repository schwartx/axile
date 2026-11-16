import { expect, test } from '@playwright/test'

test('depth rows, guide lines and quantities animate together and interrupted moves continue from the visible position', async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.goto('/tests/replay-depth.fixture.html')
  await page.getByRole('button', { name: '验收场景' }).click()
  await page.getByRole('option', { name: '盘口动效', exact: true }).click()
  await page.getByRole('button', { name: '行情帧 1', exact: true }).click()
  const row = page.locator('[data-price="99.98:99.98"]')
  const initial = (await row.boundingBox())!.y
  await page.evaluate(() => {
    const state = window as Window & { depthMotion: { holdAt: number; row: Element; transitions: string[] } }
    state.depthMotion = { holdAt: 100, row: document.querySelector('[data-price="99.98:99.98"]')!, transitions: [] }
    document.querySelector('.replay-depth-ladder')!.addEventListener('transitionrun', event => state.depthMotion.transitions.push((event as TransitionEvent).propertyName))
    const animate = Element.prototype.animate
    Element.prototype.animate = function(...args) {
      const animation = animate.apply(this, args)
      if (this.matches('.replay-ladder-list [data-price]')) { animation.pause(); animation.currentTime = state.depthMotion.holdAt }
      return animation
    }
  })
  await page.getByRole('button', { name: '行情帧 2', exact: true }).click()
  const moved = await row.evaluate(element => {
    const offset = new DOMMatrixReadOnly(getComputedStyle(element).transform).m42
    return { y: element.getBoundingClientRect().y, offset, active: element.getAnimations().length }
  })
  expect(moved.active).toBe(1)
  expect(moved.y).toBeGreaterThan(initial)
  expect(moved.y).toBeLessThan(initial + 20)
  expect(moved.offset).toBeLessThan(0)
  await expect.poll(() => page.evaluate(() => (window as Window & { depthMotion: { transitions: string[] } }).depthMotion.transitions)).toEqual(expect.arrayContaining(['width', 'd']))
  await page.evaluate(() => document.querySelector('.replay-depth-ladder')!.getAnimations({ subtree: true }).forEach(animation => { animation.pause(); animation.currentTime = 100 }))
  await page.screenshot({ path: testInfo.outputPath('depth-motion-midpoint.png'), animations: 'allow' })
  const continued = await page.evaluate(async () => {
    const state = window as Window & { depthMotion: { holdAt: number; row: Element } }
    state.depthMotion.holdAt = 0
    const before = state.depthMotion.row.getBoundingClientRect().y
    ;[...document.querySelectorAll('button')].find(button => button.textContent === '行情帧 3')!.click()
    await new Promise(requestAnimationFrame)
    const row = document.querySelector('[data-price="99.98:99.98"]')!
    return { sameNode: row === state.depthMotion.row, before, after: row.getBoundingClientRect().y }
  })
  expect(continued.sameNode).toBe(true)
  expect(Math.abs(continued.after - continued.before)).toBeLessThan(.5)
  const sameMove = await page.evaluate(async () => {
    const row = document.querySelector('[data-price="99.98:99.98"]')!, animation = row.getAnimations()[0]
    ;[...document.querySelectorAll('button')].find(button => button.textContent === '行情帧 1')!.click()
    await new Promise(requestAnimationFrame)
    return animation === row.getAnimations()[0]
  })
  expect(sameMove).toBe(true)
  await expect(page.locator('[data-price="99.99:99.99"]')).toHaveCount(0)
  await page.setViewportSize({ width: 1100, height: 800 })
  await expect.poll(() => row.evaluate(element => element.getAnimations().length)).toBe(0)
})

test('reduced motion cancels a running depth move and keeps rows and guides aligned immediately', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.goto('/tests/replay-depth.fixture.html')
  await page.getByRole('button', { name: '验收场景' }).click()
  await page.getByRole('option', { name: '盘口动效', exact: true }).click()
  await page.getByRole('button', { name: '行情帧 1', exact: true }).click()
  await page.getByRole('button', { name: '行情帧 2', exact: true }).click()
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const row = page.locator('[data-price="99.98:99.98"]')
  await expect.poll(() => row.evaluate(element => getComputedStyle(element).transform)).toBe('none')
  await page.getByRole('button', { name: '行情帧 3', exact: true }).click()
  await expect.poll(() => row.evaluate(element => element.getAnimations().length)).toBe(0)
  const state = await row.evaluate(element => ({ transform: getComputedStyle(element).transform,
    barDuration: getComputedStyle(element.querySelector('.replay-ladder-amount > i')!).transitionDuration,
    guideDuration: getComputedStyle(document.querySelector('.replay-ladder-guide')!).transitionDuration }))
  expect(state).toEqual({ transform: 'none', barDuration: '0s', guideDuration: '0s' })
})

test('own orders show separate buy and sell tags beside price in the compact ladder', async ({ page }, testInfo) => {
  await page.goto('/tests/replay-depth.fixture.html')
  await page.getByRole('button', { name: '验收场景' }).click()
  await page.getByRole('option', { name: '同价双向挂单' }).click()
  const ladder = page.getByLabel('当前盘口', { exact: true })
  const row = ladder.locator('[data-price="100:100"]')
  await expect(row.locator('.replay-ladder-order-tag')).toHaveText(['买 250', '卖 100'])
  await expect(ladder.locator('.replay-ladder-head')).toHaveText('档位价格市场量')
  await expect(ladder.locator('[data-price="99:99"] .replay-ladder-order-tag')).toHaveCount(0)
  const geometry = await row.evaluate(element => {
    const tags = [...element.querySelectorAll('.replay-ladder-order-tag')].map(tag => tag.getBoundingClientRect())
    const amount = element.querySelector('.replay-ladder-amount')!.getBoundingClientRect()
    const list = element.closest('.replay-ladder-list')!.getBoundingClientRect()
    const prices = [...element.closest('.replay-ladder-list')!.querySelectorAll('.replay-ladder-price > span:first-child')].map(price => price.getBoundingClientRect().right)
    return { separated: tags[0].right <= tags[1].left && tags[1].right <= amount.left,
      fits: amount.right <= list.right, pricesAligned: prices.every(right => right === prices[0]), height: element.getBoundingClientRect().height }
  })
  expect(geometry).toEqual({ separated: true, fits: true, pricesAligned: true, height: 20 })
  await row.getByRole('button').focus()
  await expect(page.getByLabel(/^100(?:\.00)? 的账户挂单$/)).toBeVisible()
  await page.getByRole('button', { name: /BUY-B.*买 · 剩余 50/ }).click()
  await expect(row.locator('.is-selected')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('compact-own-orders.png') })
})

test('ladder headers and all ranks align with a visible scrollbar and stay aligned after scrolling', async ({ page }, testInfo) => {
  await page.goto('/tests/replay-depth.fixture.html')
  await page.getByRole('button', { name: '验收场景' }).click()
  await page.getByRole('option', { name: '买120 / 卖120 · 聚合' }).click()
  await page.addStyleTag({ content: '.replay-ladder-list { scrollbar-gutter: stable; }' })
  const list = page.getByRole('list', { name: '当前已录价位' })
  await expect(list.getByRole('listitem')).toHaveCount(240)
  for (const dark of [true, false]) {
    await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), dark)
    await list.evaluate(element => { element.scrollTop = element.scrollHeight })
    const geometry = await list.evaluate(element => {
      const right = (node: Element) => {
        const range = document.createRange()
        range.selectNodeContents(node)
        return range.getBoundingClientRect().right
      }
      const head = element.querySelector('.replay-ladder-head')!
      const headers = [...head.children].map(right)
      const rows = [...element.querySelectorAll('.replay-ladder-row')]
      return {
        scrollbar: element.offsetWidth - element.clientWidth,
        aligned: rows.every(row => [row.children[0], row.querySelector('.replay-ladder-price > span')!, row.querySelector('.replay-ladder-amount > span')!]
          .every((cell, index) => Math.abs(right(cell) - headers[index]) < .5)),
        sticky: Math.abs(head.getBoundingClientRect().top - element.getBoundingClientRect().top) < .5,
      }
    })
    expect(geometry.scrollbar).toBeGreaterThan(0)
    expect(geometry.aligned).toBe(true)
    expect(geometry.sticky).toBe(true)
  }
  await page.screenshot({ path: testInfo.outputPath('aligned-depth-columns.png') })
})

test('large-canvas timeline drag reuses history while seeking in both directions', async ({ browser }, testInfo) => {
  const context = await browser.newContext({ viewport: { width: 2000, height: 1200 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  try {
    await page.goto('/tests/replay-depth.fixture.html')
    await page.getByRole('button', { name: '验收场景' }).click()
    await page.getByRole('option', { name: '大数据拖动 · 一万盘口与五千成交' }).click()
    await expect(page.locator('.replay-heat-scale')).toContainText('挂量峰值')
    for (const [name, dx, dy] of [['录制定位', 800, 0]] as const) {
      const bounds = (await page.getByRole('slider', { name }).boundingBox())!
      const x = bounds.x + 80, y = bounds.y + bounds.height / 2
      await page.mouse.move(x, y)
      await page.evaluate(() => {
        const state = window as Window & { resizeStats?: { frames: number[]; active: boolean; canvases: number } }
        const stats = state.resizeStats = { frames: [], active: true, canvases: 0 }
        const original = document.createElement.bind(document)
        document.createElement = ((...args: Parameters<typeof document.createElement>) => {
          if (args[0] === 'canvas' && stats.active) stats.canvases++
          return original(...args)
        }) as typeof document.createElement
        let previous = performance.now()
        const sample = (now: number) => { if (!stats.active) return; stats.frames.push(now - previous); previous = now; requestAnimationFrame(sample) }
        requestAnimationFrame(sample)
      })
      await page.mouse.down()
      await page.mouse.move(x + dx, y + dy, { steps: 20 })
      await page.mouse.move(x + dx / 4, y, { steps: 20 })
      const stats = await page.evaluate(() => {
        const stats = (window as Window & { resizeStats: { frames: number[]; active: boolean; canvases: number } }).resizeStats
        stats.active = false
        return { frames: stats.frames.sort((a, b) => a - b), canvases: stats.canvases }
      })
      await page.mouse.up()
      await expect(page.locator('.replay-heat-scale')).toContainText('挂量峰值')
      console.log(JSON.stringify({ interaction: name, frames: stats.frames.length, p95FrameMs: stats.frames[Math.floor(stats.frames.length * .95)], newHistoryCanvases: stats.canvases }))
      expect(stats.frames.length).toBeGreaterThan(3)
      expect(stats.canvases).toBe(0)
      const value = Number(await page.getByRole('slider', { name }).getAttribute('aria-valuenow'))
      expect(value).toBeGreaterThan(0)
      expect(value).toBeLessThan(500_000)
      await page.screenshot({ path: testInfo.outputPath('timeline-after-seek.png') })
    }
  } finally { await context.close() }
})
test('horizontal chart panning reuses ladder rows while vertical panning updates price guides', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await page.goto('/tests/replay-depth.fixture.html')
  const chart = page.getByRole('group', { name: /历史盘口热力图/ })
  await expect(page.locator('.replay-heat-scale')).toContainText('挂量峰值')
  await chart.focus()
  await page.keyboard.press('Equal')
  await expect(page.locator('.replay-heat-scale')).toContainText('挂量峰值')
  const bounds = (await chart.boundingBox())!
  const x = bounds.x + 150, y = bounds.y + 100
  await page.mouse.move(x, y)
  await page.mouse.down()
  await page.evaluate(() => {
    const state = window as Window & { panRowReads: number }
    state.panRowReads = 0
    const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetTop')!
    Object.defineProperty(HTMLElement.prototype, 'offsetTop', { ...descriptor, get() {
      if (this.hasAttribute('data-price')) state.panRowReads++
      return descriptor.get!.call(this)
    } })
  })
  try {
    await page.mouse.move(x + 60, y, { steps: 12 })
    await page.mouse.move(x, y, { steps: 12 })
    expect(await page.evaluate(() => (window as Window & { panRowReads: number }).panRowReads)).toBe(0)
  } finally { await page.mouse.up() }
  await expect(page.locator('.replay-heat-scale')).toContainText('挂量峰值')
  const guides = page.locator('.replay-ladder-guide')
  const before = await guides.evaluateAll(paths => paths.map(path => path.getAttribute('d')))
  await page.mouse.move(x, y)
  await page.mouse.down()
  try {
    await page.mouse.move(x, y + 25, { steps: 12 })
    await expect.poll(() => guides.evaluateAll(paths => paths.map(path => path.getAttribute('d')))).not.toEqual(before)
  } finally { await page.mouse.up() }
  await expect(page.locator('.replay-heat-scale')).toContainText('挂量峰值')
  await expect(page.getByRole('list', { name: '当前已录价位' })).toContainText('买1')
})

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
  await expect(page.locator('.replay-chart')).toBeVisible()
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
      const { wasmResource } = await import('/src/lib/wasmResources.ts')
      const [moduleBytes] = await wasmResource('analysis').read()
      await exchange({ kind: 'load', rows, wasm: moduleBytes }, [moduleBytes!, ...rows.map((row: Float64Array) => row.buffer)])
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
  await expect(page.locator('.replay-chart')).toBeVisible()
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
