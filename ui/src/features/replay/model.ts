import type {
  ReplayCommand, ReplayCursor, ReplayData, ReplayMessage, ReplayOrder, ReplayPosition,
  ReplayProjection, ReplayQuote, ReplayTrade, ReplayWindow,
} from '@/features/replay/types'

export function replayObject(value: unknown): ReplayData {
  return value != null && typeof value === 'object' && !Array.isArray(value) ? value as ReplayData : {}
}
const text = (value: unknown): string | null => typeof value === 'string' && value !== '' ? value : null
const number = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null
const integer = (value: unknown): number | null => Number.isSafeInteger(value) ? value as number : null
const rows = (value: unknown): unknown[] => Array.isArray(value) ? value : Object.values(replayObject(value))
const symbolOf = (data: ReplayData, topic = '') => text(data.symbol) ?? (topic.includes('/') ? text(topic.slice(topic.indexOf('/') + 1)) : null)
const orderKey = (symbol: string, id: string) => JSON.stringify([symbol, id])
const positionKey = (symbol: string, side: string | null) => JSON.stringify([symbol, side])
const terminalStates = new Set(['filled', 'canceled', 'cancelled', 'rejected', 'expired'])
const pendingCancelPhases = new Set(['request', 'returned', 'result', 'native_request', 'native_return', 'native_returned'])

function known(data: ReplayData, key: string): unknown {
  const quality = replayObject(data.field_quality)[key]
  return quality === 'missing' || quality === 'invalid' ? undefined : data[key]
}
function sourceTime(data: ReplayData, fallback: number): number {
  return integer(data.observed_at_us) ?? fallback
}
function mergeFields(previous: ReplayData, incoming: ReplayData): ReplayData {
  const merged = { ...previous }
  for (const [field, value] of Object.entries(incoming)) {
    if (field === 'field_quality') merged[field] = { ...replayObject(previous[field]), ...replayObject(value) }
    else if (value != null || !Object.hasOwn(previous, field)) merged[field] = value
  }
  return merged
}
function sameTradeFact(field: string, left: unknown, right: unknown): boolean {
  return ['direction', 'position_side', 'offset'].includes(field) && typeof left === 'string' && typeof right === 'string'
    ? left.toLowerCase() === right.toLowerCase() : left === right
}
function quote(data: ReplayData, time: number, seq: number, topic = ''): ReplayQuote | null {
  const symbol = symbolOf(data, topic)
  if (!symbol) return null
  const levels = (value: unknown) => rows(value).flatMap(item => {
    const row = replayObject(item), price = number(row.price), quantity = number(row.quantity)
    return price != null && price > 0 && quantity != null && quantity >= 0 ? [{ price, quantity }] : []
  })
  const bids = levels(known(data, 'bids')), asks = levels(known(data, 'asks'))
  return {
    symbol, bids, asks, book_valid: known(data, 'book_valid') === true,
    valid_until_us: integer(known(data, 'valid_until_us')),
    available_depth: number(known(data, 'available_depth')) ?? Math.min(bids.length, asks.length),
    depth_coverage: data.depth_coverage ? replayObject(data.depth_coverage) : null,
    last_price: number(known(data, 'last_price')), last_price_kind: text(data.last_price_kind) ?? 'unknown',
    time_us: sourceTime(data, time), capture_seq: seq,
  }
}
function order(data: ReplayData, time: number, seq: number, carry = false): ReplayOrder | null {
  const symbol = symbolOf(data), observedId = text(known(data, 'order_id')), id = observedId ?? `unknown-observation:${seq}`
  if (!symbol) return null
  const observedStatus = text(known(data, 'status')), normalized = observedStatus?.toLowerCase()
  const terminal = known(data, 'terminal') === true || terminalStates.has(normalized ?? '')
  const absent = !terminal && known(data, 'confirmation_state') === 'absent'
  const status = absent ? 'unknown' : observedStatus
  const confirmed = !absent && observedId != null && !id.startsWith('unknown-observation:') && !terminal && known(data, 'confirmed_book') === true && status !== 'trigger_wait'
  return {
    key: orderKey(symbol, id), order_id: id, symbol,
    price: number(known(data, 'price')), volume: number(known(data, 'quantity')),
    filled_volume: number(known(data, 'filled_quantity')), remaining_volume: number(known(data, 'remaining_quantity')),
    direction: text(known(data, 'direction')), offset: text(known(data, 'offset')), status, observed_status: observedStatus,
    confirmed, terminal, cancel_pending: !terminal && (known(data, 'cancel_pending') === true || normalized === 'pending_cancel'), carry_in: carry,
    execution_id: text(known(data, 'execution_id')), origin: text(known(data, 'origin')), previous_order_id: text(data.previous_order_id),
    time_us: sourceTime(data, time), capture_seq: seq,
    attributes: replayObject(data.attributes), native_identity: replayObject(data.native_identity),
  }
}
function trade(data: ReplayData, time: number, seq: number): ReplayTrade | null {
  const symbol = symbolOf(data)
  if (!symbol) return null
  const quality = text(data.identity_quality) ?? 'unknown'
  return {
    key: quality === 'stable' ? text(data.trade_key) : null, symbol,
    order_id: text(data.order_id), price: number(known(data, 'price')), quantity: number(known(data, 'quantity')),
    direction: text(known(data, 'direction')), offset: text(known(data, 'offset')),
    position_side: text(known(data, 'position_side')), source: text(data.source), source_seq: integer(data.source_seq),
    execution_id: text(known(data, 'execution_id')), identity_quality: quality, origin: text(known(data, 'origin')), conflict: false,
    granularity: text(data.granularity), time_us: sourceTime(data, time), capture_seq: seq,
    attributes: replayObject(data.attributes), native_identity: replayObject(data.native_identity),
  }
}
function position(data: ReplayData, time: number): ReplayPosition | null {
  const symbol = symbolOf(data)
  if (!symbol) return null
  return {
    symbol, direction: text(known(data, 'position_side')), volume: number(known(data, 'quantity')),
    available_volume: number(known(data, 'available_quantity')), frozen_volume: number(known(data, 'frozen_quantity')),
    today_volume: number(known(data, 'today_quantity')), yesterday_volume: number(known(data, 'yesterday_quantity')),
    time_us: sourceTime(data, time), attributes: replayObject(data.attributes),
  }
}

type TradeFact = ReplayTrade & { data: ReplayData }
type ProjectionState = {
  view: ReplayProjection
  ownSeen: Set<string>
  marketSeen: Set<string>
  ownFacts: Map<string, TradeFact>
  marketFacts: Map<string, TradeFact>
  orderChanges: Record<string, number>
  setRevisions: Record<string, number>
  setObservations: Record<string, [number, number, number]>
  generations: Record<string, number>
  knownScopes: Set<string>
  basis: Record<string, ReplayData>
  executions: Record<string, ReplayData>
  pending: Map<string, ReplayData>
  orderFacts: Record<string, ReplayData>
  positionFacts: Record<string, ReplayData>
  ownLastFactSeq: Map<string, number>
  commandFacts: Record<string, ReplayData>
  commandOwners: Map<string, string | null>
  cancelOrders: Set<string>
}
function scopeSymbols(scope: unknown, state: ProjectionState): Set<string> {
  if (Array.isArray(scope)) {
    const symbols = new Set(scope.filter((value): value is string => typeof value === 'string' && value !== ''))
    if (symbols.delete('*')) for (const symbol of scopeSymbols('account', state)) symbols.add(symbol)
    return symbols
  }
  const object = replayObject(scope)
  if (scope === 'account' || object.kind === 'account') return new Set([
    ...Object.values(state.view.orders).map(value => value.symbol),
    ...Object.values(state.view.observedPositions).map(value => value.symbol),
    ...Object.keys(state.view.quotes),
    ...Object.keys(state.generations),
    ...state.knownScopes,
    ...rows(object.symbols).filter((value): value is string => typeof value === 'string' && value !== ''),
  ])
  if (Array.isArray(object.symbols)) return scopeSymbols(object.symbols, state)
  return new Set()
}
function accountScope(scope: unknown): boolean {
  const object = replayObject(scope)
  return scope === 'account' || object.kind === 'account'
    || (Array.isArray(scope) ? scope : rows(object.symbols)).includes('*')
}
function warn(state: ProjectionState, message: string) {
  if (!state.view.warnings.includes(message)) state.view.warnings.push(message)
}
function seed(window: ReplayWindow): ProjectionState {
  const base = replayObject(window.state_before_start), accounting = replayObject(base.trade_accounting)
  const seq = integer(base.state_through_seq) ?? 0
  const ownSeen = new Set(rows(accounting.own_seen_keys).filter((value): value is string => typeof value === 'string'))
  const marketSeen = new Set(rows(accounting.market_seen_keys).filter((value): value is string => typeof value === 'string'))
  const view: ReplayProjection = {
    recording_id: window.recording_id, time_us: integer(base.time_us) ?? 0, capture_seq: seq,
    quotes: {}, orders: {}, ownTrades: [], marketTrades: [], commands: [], targets: {},
    observedPositions: {}, derivedPositions: {}, executionTotals: {}, executionComplete: {}, executionSymbolComplete: {},
    targetsByExecution: {}, orderHistory: [], quoteHistory: [],
    accountingComplete: accounting.own_complete === true,
    warnings: [],
  }
  const state: ProjectionState = {
    view, ownSeen, marketSeen, ownFacts: new Map(), marketFacts: new Map(), orderChanges: {}, setRevisions: {}, setObservations: {}, generations: {}, knownScopes: new Set(), basis: {}, executions: {}, pending: new Map(),
    orderFacts: {}, positionFacts: {}, ownLastFactSeq: new Map(), commandFacts: {}, commandOwners: new Map(), cancelOrders: new Set(),
  }
  for (const item of rows(base.quotes)) {
    const row = replayObject(item), value = quote(row, view.time_us, integer(row.observed_seq) ?? seq)
    if (value) { view.quotes[value.symbol] = value; view.quoteHistory.push(value) }
  }
  for (const item of rows(base.orders)) {
    const row = replayObject(item), value = order(row, view.time_us, integer(row.observed_seq) ?? seq, true)
    if (value) { state.orderFacts[value.key] = { ...row }; view.orders[value.key] = value; view.orderHistory.push(value) }
  }
  for (const [execution, value] of Object.entries(replayObject(base.executions))) {
    state.executions[execution] = replayObject(value)
    view.targetsByExecution[execution] = {}
    const data = replayObject(value)
    for (const [symbol, raw] of Object.entries(replayObject(data.by_symbol))) {
      const target = number(replayObject(raw).target_quantity)
      if (target != null) { view.targets[symbol] = target; view.targetsByExecution[execution][symbol] = target }
    }
  }
  for (const [input, output] of [[base.observed_positions, view.observedPositions], [base.derived_positions, view.derivedPositions]] as const) {
    for (const raw of rows(input)) {
      const row = replayObject(raw), value = position(row, view.time_us)
      if (value) {
        const key = positionKey(value.symbol, value.direction)
        output[key] = value
        if (output === view.observedPositions) state.positionFacts[key] = { ...row }
      }
    }
  }
  for (const [input, output] of [[base.order_change_seq, state.orderChanges], [base.last_set_revision, state.setRevisions], [base.sync_generation, state.generations]] as const) {
    for (const [key, value] of Object.entries(replayObject(input))) if (integer(value) != null) output[key] = value as number
  }
  for (const [scope, value] of Object.entries(replayObject(base.last_set_observation))) {
    if (Array.isArray(value) && value.length === 3 && integer(value[0]) != null && value[0] >= 0
      && integer(value[1]) != null && value[1] > 0 && integer(value[2]) != null && value[2] >= 0) state.setObservations[scope] = [value[0], value[1], value[2]]
  }
  for (const [symbol, value] of Object.entries(replayObject(base.position_basis))) state.basis[symbol] = replayObject(value)
  for (const field of ['connections', 'position_scopes'] as const) {
    for (const symbol of Object.keys(replayObject(base[field]))) if (symbol) state.knownScopes.add(symbol)
  }
  for (const key of Object.keys(state.basis)) {
    try {
      const value: unknown = JSON.parse(key)
      if (Array.isArray(value) && typeof value[0] === 'string' && value[0]) state.knownScopes.add(value[0])
    } catch { continue }
  }
  for (const [input, output, visible] of [[accounting.own_records, state.ownFacts, view.ownTrades], [accounting.market_records, state.marketFacts, view.marketTrades]] as const) {
    for (const raw of rows(input)) {
      const record = replayObject(raw), data = replayObject(record.data)
      const value = trade(data, integer(record.first_time_us) ?? view.time_us, integer(record.first_capture_seq) ?? seq)
      if (value?.key) {
        value.conflict = record.conflict === true; output.set(value.key, { ...value, data }); visible.push(value)
        if (output === state.ownFacts) state.ownLastFactSeq.set(value.key, integer(record.last_fact_seq) ?? value.capture_seq)
      } else if (data.identity_quality === 'stable' && text(data.trade_key)) {
        state.pending.set(`${output === state.ownFacts ? 'own' : 'market'}:trade:${data.trade_key}`, { market: output === state.marketFacts, observation: record })
        if (output === state.ownFacts) view.accountingComplete = false
      }
    }
  }
  for (const [key, raw] of Object.entries(replayObject(accounting.pending))) {
    const pending = { ...replayObject(raw) }
    state.pending.set(key, pending)
    const observation = replayObject(pending.observation), nested = Object.hasOwn(observation, 'data')
    const data = nested ? replayObject(observation.data) : observation
    const value = trade(data, integer(nested ? observation.first_time_us : observation.observed_at_us) ?? view.time_us,
      integer(nested ? observation.first_capture_seq : observation.observed_seq) ?? seq)
    const visible = pending.market === true ? view.marketTrades : view.ownTrades
    if (value && !visible.some(item => value.key ? item.key === value.key : item.capture_seq === value.capture_seq && item.key == null)) visible.push(value)
  }
  const commands = rows(base.commands).map(replayObject)
  for (const data of commands) {
    rememberCommand(state, data, integer(data.observed_at_us) ?? view.time_us, integer(data.observed_seq) ?? seq)
  }
  bindCommandOwners(state)
  rebuildCancelPending(state)
  if (!view.accountingComplete) warn(state, '账户历史成交覆盖不完整，单次执行按各自记录判断')
  rebuildOrderAmounts(state)
  view.orderHistory = Object.values(view.orders)
  return state
}

function updateOrder(state: ProjectionState, data: ReplayData, message: ReplayMessage, snapshotStart?: number, snapshotIsChange = false) {
  const symbol = symbolOf(data), observedId = text(known(data, 'order_id')), id = observedId ?? `unknown-observation:${message.capture_seq}`
  if (!symbol) { warn(state, '委托观察缺少品种'); return }
  if (!observedId) warn(state, '部分委托缺少稳定身份，仅展示未知观察')
  const key = orderKey(symbol, id)
  const generation = integer(data.sync_generation)
  if (snapshotStart != null && snapshotIsChange && generation != null && [state.setObservations[symbol], state.setObservations['']]
    .some(observation => observation && (observation[0] > generation || observation[0] === generation && observation[2] > snapshotStart))) return
  if (!acceptGeneration(state, symbol, generation, message)) return
  if (snapshotStart != null && (state.orderChanges[key] ?? -1) > snapshotStart) return
  const previous = state.view.orders[key], raw = state.orderFacts[key] ?? {}
  if (generation == null && previous) {
    state.orderFacts[key] = { ...raw, confirmed_book: false }
    state.view.orders[key] = { ...previous, confirmed: false, time_us: message.time_us, capture_seq: message.capture_seq }
    state.view.orderHistory.push(state.view.orders[key]); state.orderChanges[key] = message.capture_seq
    return
  }
  const incoming = observedId ? bindKnownOwner(state, data) : { ...data, order_id: id, confirmed_book: false }
  const merged = mergeFields(raw, incoming)
  const incomingOrder = order(incoming, message.time_us, message.capture_seq)
  if (previous?.terminal && !incomingOrder?.terminal) {
    for (const field of ['status', 'remaining_quantity', 'confirmed_book'] as const) merged[field] = raw[field]
  }
  const previousFilled = number(known(raw, 'observed_filled_quantity')) ?? number(known(raw, 'filled_quantity'))
  const filled = number(known(incoming, 'filled_quantity'))
  if (previousFilled != null && filled != null && filled < previousFilled) {
    for (const field of ['filled_quantity', 'remaining_quantity', 'status'] as const) merged[field] = raw[field]
  } else if (filled != null) {
    merged.observed_filled_quantity = filled
    if (filled === 0) merged.fill_start_observed = true
  }
  if (number(known(incoming, 'remaining_quantity')) != null) merged.observed_remaining_quantity = incoming.remaining_quantity
  merged.confirmed_book = observedId != null && generation != null && (Object.hasOwn(incoming, 'confirmed_book')
    ? known(incoming, 'confirmed_book') === true : raw.confirmed_book === true)
  merged.confirmation_state = terminalStates.has(text(known(merged, 'status'))?.toLowerCase() ?? '') ? 'terminal'
    : merged.confirmed_book ? 'confirmed' : 'unknown'
  merged.observed_at_us = message.time_us; merged.observed_seq = message.capture_seq
  state.orderFacts[key] = merged
  const value = order(merged, message.time_us, message.capture_seq)!
  value.cancel_pending = cancelPending(state, value)
  state.orderFacts[key].cancel_pending = value.cancel_pending
  state.view.orders[key] = { ...value, carry_in: previous?.carry_in ?? false }
  if (snapshotStart == null || snapshotIsChange) state.orderChanges[key] = message.capture_seq
  rebuildOrderAmounts(state)
  state.view.orderHistory.push(state.view.orders[key])
}
function acceptGeneration(state: ProjectionState, symbol: string, generation: number | null, message: ReplayMessage): boolean {
  if (generation == null) return true
  const previous = state.generations[symbol]
  if (previous != null && generation < previous) return false
  if (previous == null || generation > previous) {
    for (const [key, value] of Object.entries(state.view.orders)) {
      if (value.symbol !== symbol || !value.confirmed) continue
      state.orderFacts[key] = { ...state.orderFacts[key], confirmed_book: false }
      state.view.orders[key] = { ...value, confirmed: false, time_us: message.time_us, capture_seq: message.capture_seq }
      state.view.orderHistory.push(state.view.orders[key])
    }
    state.generations[symbol] = generation
    delete state.setRevisions[symbol]
    if ((state.setObservations[symbol]?.[0] ?? generation) < generation) delete state.setObservations[symbol]
  }
  return true
}
export function replayCommandFailed(data: ReplayData): boolean {
  return ['error', 'native_error'].includes(text(known(data, 'phase')) ?? '')
    || known(data, 'success') === false || known(data, 'accepted') === false
}
function cancelPending(state: ProjectionState, value: ReplayOrder): boolean {
  if (value.terminal) return false
  if (text(known(state.orderFacts[value.key] ?? {}, 'status'))?.toLowerCase() === 'pending_cancel') return true
  return state.cancelOrders.has(value.key)
}
function rebuildCancelPending(state: ProjectionState, message?: ReplayMessage) {
  state.cancelOrders.clear()
  for (const data of Object.values(state.commandFacts)) {
    const symbol = text(known(data, 'symbol')), id = text(known(data, 'order_id'))
    if (symbol && id && text(known(data, 'command_id')) && known(data, 'operation') === 'cancel_order'
      && pendingCancelPhases.has(text(known(data, 'phase')) ?? '') && !replayCommandFailed(data)) state.cancelOrders.add(orderKey(symbol, id))
  }
  for (const [key, previous] of Object.entries(state.view.orders)) {
    const pending = cancelPending(state, previous)
    state.orderFacts[key] = { ...state.orderFacts[key], cancel_pending: pending }
    if (pending === previous.cancel_pending) continue
    const value = { ...previous, cancel_pending: pending,
      ...(message ? { time_us: message.time_us, capture_seq: message.capture_seq } : {}) }
    state.view.orders[key] = value
    if (message) state.view.orderHistory.push(value)
  }
}
function rememberCommand(state: ProjectionState, data: ReplayData, time: number, seq: number) {
  const suppliedId = text(data.command_id), id = suppliedId ?? `observation:${seq}`
  if (!suppliedId) warn(state, '部分操作缺少命令身份，仅展示本次观察')
  const merged = mergeFields(state.commandFacts[id] ?? {}, data)
  state.commandFacts[id] = merged
  const command: ReplayCommand = {
    command_id: id, operation: text(data.operation) ?? 'unknown', phase: text(data.phase) ?? 'unknown',
    symbol: text(data.symbol), order_id: text(data.order_id), price: number(known(data, 'price')), volume: number(known(data, 'quantity')),
    time_us: time, capture_seq: seq, data: merged,
  }
  state.view.commands.push(command)
}
function applyCommand(state: ProjectionState, data: ReplayData, message: ReplayMessage) {
  rememberCommand(state, data, message.time_us, message.capture_seq)
  rebuildCancelPending(state, message)
  bindCommandOwners(state)
  rebuildOrderAmounts(state, message)
}
function bindCommandOwners(state: ProjectionState) {
  state.commandOwners.clear()
  for (const data of Object.values(state.commandFacts)) {
    if (known(data, 'operation') !== 'place_order') continue
    const symbol = text(known(data, 'symbol')), id = text(known(data, 'order_id')), execution = text(known(data, 'execution_id'))
    if (!symbol || !id || !execution) continue
    const key = orderKey(symbol, id)
    state.commandOwners.set(key, !state.commandOwners.has(key) || state.commandOwners.get(key) === execution ? execution : null)
  }
  for (const value of Object.values(state.view.orders)) {
    const owner = ownerForOrder(state, value.symbol, value.order_id)
    if (owner && !value.execution_id && !['manual', 'external'].includes(text(state.orderFacts[value.key]?.origin) ?? '')) {
      state.orderFacts[value.key] = { ...state.orderFacts[value.key], execution_id: owner, origin: 'execution' }
      state.view.orders[value.key] = { ...value, execution_id: owner, origin: 'execution' }
    }
  }
  for (const [key, value] of state.ownFacts) {
    const owner = value.order_id ? ownerForOrder(state, value.symbol, value.order_id) : null
    if (!owner || value.execution_id || ['manual', 'external'].includes(value.origin ?? '')) continue
    const { data, ...projected } = value
    const associated = { ...projected, execution_id: owner, origin: 'execution' }
    state.ownFacts.set(key, { ...associated, data: bindKnownOwner(state, data) })
    const index = state.view.ownTrades.findIndex(item => item.key === key)
    if (index >= 0) state.view.ownTrades[index] = associated
    if (validTrade(associated) && associated.order_id) state.pending.delete(`own:trade:${key}`)
  }
  for (const pending of state.pending.values()) {
    if (pending.market === true) continue
    const observation = replayObject(pending.observation), nested = Object.hasOwn(observation, 'data')
    const pendingData = nested ? replayObject(observation.data) : observation
    const associated = bindKnownOwner(state, pendingData)
    pending.observation = nested ? { ...observation, data: associated } : associated
  }
}
function ownerForOrder(state: ProjectionState, symbol: string, id: string): string | null {
  if (id.startsWith('unknown-observation:')) return null
  return state.commandOwners.get(orderKey(symbol, id)) ?? null
}
function bindKnownOwner(state: ProjectionState, data: ReplayData): ReplayData {
  if (text(known(data, 'execution_id')) || ['manual', 'external'].includes(text(known(data, 'origin')) ?? '')) return data
  const symbol = symbolOf(data), id = text(known(data, 'order_id')), owner = symbol && id ? ownerForOrder(state, symbol, id) : null
  return owner ? { ...data, execution_id: owner, origin: 'execution', field_quality: { ...replayObject(data.field_quality), execution_id: 'known', origin: 'known' } } : data
}
const validTrade = (value: ReplayTrade) => !value.conflict && value.quantity != null && value.quantity > 0
  && value.price != null && Number.isFinite(value.quantity * value.price)
function applyTrade(state: ProjectionState, data: ReplayData, message: ReplayMessage, market: boolean) {
  if (!market) data = bindKnownOwner(state, data)
  const prefix = market ? 'market' : 'own', facts = market ? state.marketFacts : state.ownFacts
  const key = data.identity_quality === 'stable' ? text(data.trade_key) : null
  const previous = key ? facts.get(key) : undefined
  const pending = key ? replayObject(state.pending.get(`${prefix}:trade:${key}`)?.observation) : {}
  const raw = previous?.data ?? replayObject(pending.data ?? pending), mergedData = mergeFields(raw, data)
  for (const [field, value] of Object.entries(raw)) {
    const quality = replayObject(data.field_quality)[field]
    if (value != null && known(raw, field) != null && (quality === 'missing' || quality === 'invalid')) {
      mergedData[field] = value
      mergedData.field_quality = { ...replayObject(mergedData.field_quality), [field]: 'known' }
    }
  }
  let conflict = previous?.conflict === true || pending.conflict === true
  for (const field of ['symbol', 'quantity', 'price', 'direction', 'position_side', 'offset']) {
    if (known(raw, field) != null && known(data, field) != null && !sameTradeFact(field, raw[field], data[field])) {
      conflict = true; mergedData[field] = raw[field]
    }
  }
  const time = previous?.time_us ?? integer(pending.first_time_us) ?? message.time_us
  const seq = previous?.capture_seq ?? integer(pending.first_capture_seq) ?? message.capture_seq
  const value = trade(mergedData, time, seq)
  if (!value) {
    if (!market) state.view.accountingComplete = false
    if (key) state.pending.set(`${prefix}:trade:${key}`, { market, observation: { data: mergedData, first_time_us: time, first_capture_seq: seq, conflict } })
    warn(state, '成交观察缺少品种'); return
  }
  const visible = market ? state.view.marketTrades : state.view.ownTrades
  if (!value.key) {
    if (!market) state.view.accountingComplete = false
    warn(state, market ? '部分市场成交身份或价量未知' : '部分自有成交身份或价量未知，累计待确认')
    visible.push(value)
    state.pending.set(`${market ? 'market' : 'own'}:observation:${message.capture_seq}`, { market, observation: data })
    if (!market) rebuildOrderAmounts(state, message)
    return
  }
  const seen = market ? state.marketSeen : state.ownSeen
  const merged = { ...value, conflict }
  if (previous) {
    const index = visible.findIndex(item => item.key === value.key)
    if (index >= 0) visible[index] = merged
  } else if (!seen.has(value.key) || !visible.some(item => item.key === value.key)) visible.push(merged)
  if (validTrade(merged)) seen.add(value.key)
  else { if (!market) state.view.accountingComplete = false; warn(state, '部分成交事实无效或冲突，数量待确认') }
  facts.set(value.key, { ...merged, data: mergedData })
  if (!market && (!previous || ['symbol', 'price', 'quantity', 'direction', 'position_side', 'offset', 'order_id'].some(field =>
    !sameTradeFact(field, known(raw, field) ?? null, known(mergedData, field) ?? null)))) state.ownLastFactSeq.set(value.key, message.capture_seq)
  if (integer(data.related_capture_seq) != null) {
    state.pending.delete(`${prefix}:observation:${data.related_capture_seq}`)
    const index = visible.findIndex(item => item.key == null && item.capture_seq === data.related_capture_seq)
    if (index >= 0) visible.splice(index, 1)
  }
  if (!validTrade(merged) || (!market && (!merged.order_id || (!merged.execution_id && !['manual', 'external'].includes(merged.origin ?? ''))))) {
    state.pending.set(`${prefix}:trade:${value.key}`, { market, observation: { ...mergedData, execution_id: merged.execution_id } })
  } else state.pending.delete(`${prefix}:trade:${value.key}`)
  if (!market) rebuildOrderAmounts(state, message)
}
function rebuildOrderAmounts(state: ProjectionState, message?: ReplayMessage) {
  const amounts = new Map<string, { quantity: number; complete: boolean; lastFact: number }>()
  const amountFor = (key: string) => {
    let amount = amounts.get(key)
    if (!amount) { amount = { quantity: 0, complete: true, lastFact: 0 }; amounts.set(key, amount) }
    return amount
  }
  for (const [key, value] of state.ownFacts) {
    if (!value.order_id) continue
    const amount = amountFor(orderKey(value.symbol, value.order_id))
    amount.lastFact = Math.max(amount.lastFact, state.ownLastFactSeq.get(key) ?? 0)
    if (validTrade(value)) amount.quantity += value.quantity!
    else amount.complete = false
    if (!Number.isFinite(amount.quantity)) amount.complete = false
  }
  for (const pending of state.pending.values()) {
    if (pending.market === true) continue
    const observation = replayObject(pending.observation), data = Object.hasOwn(observation, 'data') ? replayObject(observation.data) : observation
    const symbol = symbolOf(data), id = text(data.order_id)
    if (symbol && id && !text(data.trade_key)) amountFor(orderKey(symbol, id)).complete = false
  }
  for (const [key, previous] of Object.entries(state.view.orders)) {
    const raw = state.orderFacts[key] ?? {}, amount = amounts.get(key) ?? { quantity: 0, complete: true, lastFact: 0 }
    const observed = number(known(raw, 'observed_filled_quantity')) ?? number(known(raw, 'filled_quantity'))
    const filled = observed == null ? amount.quantity > 0 ? amount.quantity : null : Math.max(observed, amount.quantity)
    const quantity = number(known(raw, 'quantity'))
    const computed = quantity != null && filled != null ? quantity - filled : null
    const remaining = amount.complete ? computed != null && Number.isFinite(computed) && computed >= 0
      ? computed : number(known(raw, 'observed_remaining_quantity')) : null
    const next = { ...previous, filled_volume: filled, remaining_volume: remaining }
    state.orderFacts[key] = { ...raw, filled_quantity: filled, remaining_quantity: remaining,
      observed_filled_quantity: observed, known_trade_quantity: Number.isFinite(amount.quantity) ? amount.quantity : null }
    if (amount.lastFact > 0) state.orderChanges[key] = Math.max(state.orderChanges[key] ?? 0, amount.lastFact)
    if (message && (previous.filled_volume !== filled || previous.remaining_volume !== remaining)) {
      next.time_us = message.time_us; next.capture_seq = message.capture_seq
      state.view.orderHistory.push(next)
    }
    state.view.orders[key] = next
  }
}
function rebuildTotals(state: ProjectionState) {
  state.view.executionTotals = {}
  state.view.executionComplete = {}
  state.view.executionSymbolComplete = {}
  for (const [execution, data] of Object.entries(state.executions)) {
    const complete = data.start_observed === true
    state.view.executionComplete[execution] = complete
    const symbols = rows(data.symbols).filter((value): value is string => typeof value === 'string')
    if (text(data.symbol)) symbols.push(data.symbol as string)
    symbols.push(...Object.keys(replayObject(data.by_symbol)))
    state.view.executionTotals[execution] = Object.fromEntries(symbols.map(symbol => [symbol, 0]))
    state.view.executionSymbolComplete[execution] = Object.fromEntries(symbols.map(symbol => [symbol, complete]))
  }
  for (const value of state.ownFacts.values()) {
    if (!value.execution_id) continue
    const totals = state.view.executionTotals[value.execution_id] ??= {}
    state.view.executionComplete[value.execution_id] ??= false
    const completeness = state.view.executionSymbolComplete[value.execution_id] ??= {}
    completeness[value.symbol] ??= state.view.executionComplete[value.execution_id]
    if (!validTrade(value)) {
      state.view.executionComplete[value.execution_id] = false
      completeness[value.symbol] = false
      continue
    }
    totals[value.symbol] = (totals[value.symbol] ?? 0) + value.quantity!
  }
  for (const raw of state.pending.values()) {
    if (raw.market === true) continue
    const observation = replayObject(raw.observation), data = Object.hasOwn(observation, 'data') ? replayObject(observation.data) : observation
    const execution = text(data.execution_id)
    const symbol = text(data.symbol)
    const markIncomplete = (execution: string) => {
      const flags = state.view.executionSymbolComplete[execution] ??= {}
      for (const key of Object.keys(flags)) if (!symbol || key === symbol) flags[key] = false
      if (!symbol || Object.hasOwn(flags, symbol)) state.view.executionComplete[execution] = false
    }
    if (execution) markIncomplete(execution)
    else if (!['manual', 'external'].includes(text(data.origin) ?? '')) {
      for (const execution of Object.keys(state.view.executionComplete)) markIncomplete(execution)
    }
  }
}
function applyObservation(state: ProjectionState, data: ReplayData, message: ReplayMessage) {
  const positions = rows(data.positions).flatMap(raw => {
    const value = position(replayObject(raw), message.time_us)
    return value ? [value] : []
  })
  const symbols = scopeSymbols(data.scope, state)
  for (const value of positions) symbols.add(value.symbol)
  for (const symbol of symbols) state.knownScopes.add(symbol)
  if (data.scope_complete === true) {
    for (const [key, value] of Object.entries(state.view.observedPositions)) {
      if (symbols.has(value.symbol) && !positions.some(next => positionKey(next.symbol, next.direction) === key)) {
        const raw: ReplayData = { ...state.positionFacts[key], field_quality: { ...replayObject(state.positionFacts[key]?.field_quality) }, observed_at_us: message.time_us }
        for (const field of ['quantity', 'available_quantity', 'frozen_quantity', 'today_quantity', 'yesterday_quantity']) {
          if (Object.hasOwn(raw, field)) { raw[field] = 0; replayObject(raw.field_quality)[field] = 'known' }
        }
        state.positionFacts[key] = raw
        state.view.observedPositions[key] = position(raw, message.time_us)!
        state.basis[key] = { status: 'unverified', proof: null }
      }
    }
  }
  for (const raw of rows(data.positions).map(replayObject)) {
    const value = position(raw, message.time_us)
    if (!value) continue
    const key = positionKey(value.symbol, value.direction)
    const merged = { ...mergeFields(state.positionFacts[key] ?? {}, raw), observed_at_us: message.time_us }
    state.positionFacts[key] = merged
    state.view.observedPositions[key] = position(merged, message.time_us)!
  }
  for (const symbol of symbols) {
    for (const [key, value] of Object.entries(state.view.derivedPositions)) if (value.symbol === symbol) delete state.view.derivedPositions[key]
  }
  for (const raw of rows(data.positions)) {
    const row = replayObject(raw), symbol = symbolOf(row)
    if (!symbol) continue
    state.basis[positionKey(symbol, text(row.position_side))] = replayObject(row.position_basis ?? data.position_basis)
  }
}
function orderQueryBasis(data: ReplayData, message: ReplayMessage, requireIdentity = false) {
  const basis = replayObject(data.query_basis), start = integer(basis.start_seq), revision = integer(basis.request_revision)
  const generation = integer(data.sync_generation)
  if (start == null || start < 0 || start >= message.capture_seq || revision == null || revision <= 0 || generation == null) return null
  if (requireIdentity && basis.recording_id !== message.recording_id) return null
  if ((basis.recording_id != null && basis.recording_id !== message.recording_id)
    || (basis.sync_generation != null && basis.sync_generation !== generation)) return null
  return { start, revision, generation }
}
function applyOrder(state: ProjectionState, data: ReplayData, message: ReplayMessage) {
  if (!Object.hasOwn(data, 'query_basis')) { updateOrder(state, data, message); return }
  const basis = orderQueryBasis(data, message, true)
  // 接受的单笔查询也是变化，已在它之前发起的并发查询不能再回退该单。
  if (basis) updateOrder(state, data, message, basis.start, true)
}
function rememberOrderSet(state: ProjectionState, scope: string, generation: number, revision: number, seq: number) {
  const previous = state.setObservations[scope]
  if (!previous || generation > previous[0] || generation === previous[0] && revision > previous[1]) state.setObservations[scope] = [generation, revision, seq]
}
function applyOrderSet(state: ProjectionState, data: ReplayData, message: ReplayMessage) {
  if (data.synchronized !== true || data.scope_complete !== true) return
  const basis = orderQueryBasis(data, message)
  if (!basis) return
  const { start, revision, generation } = basis
  const account = state.setObservations['']
  if (account && (account[0] > generation || account[0] === generation && account[1] >= revision)) return
  const orders = rows(data.orders).map(replayObject), symbols = scopeSymbols(data.scope, state)
  for (const row of orders) { const symbol = symbolOf(row); if (symbol) symbols.add(symbol) }
  const global = accountScope(data.scope) && [...symbols].every(symbol => {
    const current = state.generations[symbol]
    return (current == null || generation >= current) && (current !== generation || revision > (state.setRevisions[symbol] ?? -1))
  })
  for (const symbol of symbols) {
    if (!acceptGeneration(state, symbol, generation, message) || revision <= (state.setRevisions[symbol] ?? -1)) continue
    state.setRevisions[symbol] = revision
    rememberOrderSet(state, symbol, generation, revision, message.capture_seq)
    const scoped = orders.filter(row => symbolOf(row) === symbol)
    for (const row of scoped) updateOrder(state, { ...row, sync_generation: generation }, message, start)
    const present = new Set(scoped.map(row => text(row.order_id)).filter(Boolean))
    for (const [key, previous] of Object.entries(state.view.orders)) {
      if (previous.symbol === symbol && !previous.terminal && !present.has(previous.order_id) && (state.orderChanges[key] ?? -1) <= start) {
        state.orderFacts[key] = { ...state.orderFacts[key], confirmed_book: false, confirmation_state: 'absent' }
        if (previous.confirmed || previous.status !== 'unknown') {
          state.view.orders[key] = { ...previous, confirmed: false, status: 'unknown' }
          state.view.orderHistory.push({ ...state.view.orders[key], time_us: message.time_us, capture_seq: message.capture_seq })
        }
      }
    }
  }
  if (global) rememberOrderSet(state, '', generation, revision, message.capture_seq)
}
function applyConnection(state: ProjectionState, data: ReplayData, message: ReplayMessage) {
  const disconnected = ['connected', 'ready', 'available', 'synchronized'].some(field => data[field] === false)
    || ['disconnected', 'unavailable', 'invalidated'].includes(text(data.status) ?? '')
  const scope = data.scope ?? 'account', fullAccount = accountScope(scope)
  const symbols = scopeSymbols(scope, state), generation = integer(data.sync_generation)
  for (const symbol of symbols) {
    if (!acceptGeneration(state, symbol, generation, message)) continue
    state.knownScopes.add(symbol)
    if (!disconnected) continue
    if (!fullAccount && generation != null && generation >= (state.setObservations[symbol]?.[0] ?? generation)) delete state.setObservations[symbol]
    for (const [key, value] of Object.entries(state.view.orders)) if (value.symbol === symbol) {
      state.orderFacts[key] = { ...state.orderFacts[key], confirmed_book: false }
      state.view.orders[key] = { ...value, confirmed: false, time_us: message.time_us, capture_seq: message.capture_seq }
      state.view.orderHistory.push(state.view.orders[key])
    }
    if (state.view.quotes[symbol]) {
      state.view.quotes[symbol] = { ...state.view.quotes[symbol], book_valid: false, time_us: message.time_us, capture_seq: message.capture_seq }
      state.view.quoteHistory.push(state.view.quotes[symbol])
    }
  }
  if (disconnected && fullAccount && generation != null
    && Object.values(state.generations).every(current => generation >= current)
    && Object.values(state.setObservations).every(observation => generation >= observation[0])) state.setObservations = {}
}
function derivePositions(state: ProjectionState) {
  state.view.derivedPositions = {}
  for (const [key, observed] of Object.entries(state.view.observedPositions)) {
    const basis = state.basis[key], proof = replayObject(basis?.proof)
    const keysProof = proof.kind === 'included_trade_keys' && proof.complete === true && Array.isArray(proof.included_trade_keys)
    const watermarkProof = proof.kind === 'source_watermark' && text(proof.source) != null && integer(proof.through) != null
    if (basis?.status !== 'aligned' || (!keysProof && !watermarkProof) || observed.volume == null) continue
    const included = new Set(rows(proof.included_trade_keys).filter((value): value is string => typeof value === 'string'))
    let amount = observed.volume, valid = true
    for (const value of state.ownFacts.values()) {
      if (value.symbol !== observed.symbol || !value.key || included.has(value.key)) continue
      if (value.conflict) { valid = false; break }
      if (watermarkProof) {
        if (value.source !== proof.source || value.source_seq == null) { valid = false; break }
        if (value.source_seq <= (proof.through as number)) continue
      }
      const direction = value.direction?.toLowerCase(), offset = value.offset?.toLowerCase(), side = observed.direction?.toLowerCase()
      if (value.quantity == null || !['buy', 'sell'].includes(direction ?? '') || !['net', 'long', 'short'].includes(value.position_side?.toLowerCase() ?? '')) { valid = false; break }
      if (value.position_side?.toLowerCase() !== side) continue
      if (side === 'net') amount += direction === 'buy' ? value.quantity : -value.quantity
      else if (['long', 'short'].includes(side ?? '') && ['open', 'close', 'closetoday', 'closeyesterday'].includes(offset ?? '')) {
        if ((direction === 'buy') !== ((side === 'long') === (offset === 'open'))) { valid = false; break }
        amount += offset === 'open' ? value.quantity : -value.quantity
      } else { valid = false; break }
    }
    for (const pending of state.pending.values()) {
      if (pending.market === true) continue
      const observation = replayObject(pending.observation), data = Object.hasOwn(observation, 'data') ? replayObject(observation.data) : observation
      if (data.symbol === observed.symbol && !text(data.trade_key)) valid = false
    }
    if (valid && (observed.direction?.toLowerCase() === 'net' || amount >= 0)) state.view.derivedPositions[key] = {
      ...observed, volume: amount, available_volume: null, frozen_volume: null,
      today_volume: amount === observed.volume ? observed.today_volume : null,
      yesterday_volume: amount === observed.volume ? observed.yesterday_volume : null,
    }
  }
}

/** 从完整窗口基线重建到游标；反向 seek 不尝试倒减交易事实。 */
export function projectReplay(window: ReplayWindow, cursor: ReplayCursor, _symbol?: string): ReplayProjection {
  const state = seed(window), seenSequences = new Set<number>()
  const baseline = replayObject(window.state_before_start)
  if (text(baseline.recording_id) && baseline.recording_id !== window.recording_id) throw new Error('回放基线不属于当前录制段')
  if (baseline.schema_version != null && baseline.schema_version !== 1) throw new Error('不支持此回放基线版本')
  if (state.view.time_us > cursor.time_us || (cursor.capture_seq != null && cursor.capture_seq < state.view.capture_seq)) throw new Error('游标早于窗口基线，请重新加载时间窗口')
  let last = state.view.capture_seq
  let previousTime = integer(baseline.time_us)
  for (const message of window.messages) {
    if (message.recording_id !== window.recording_id) throw new Error('回放窗口混入其他录制段')
    if (message.schema_version !== 1 || !Number.isSafeInteger(message.time_us) || message.time_us < 0
      || !Number.isSafeInteger(message.capture_seq) || message.capture_seq <= 0) throw new Error('回放事件版本或时间无效')
    if (seenSequences.has(message.capture_seq) || message.capture_seq <= (integer(baseline.state_through_seq) ?? 0)) continue
    if (message.capture_seq < last) throw new Error('回放观察顺序回退')
    if (previousTime != null && message.time_us < previousTime) throw new Error('当前录制段的观察时间回退')
    if (message.time_us > cursor.time_us || (message.time_us === cursor.time_us && cursor.capture_seq != null && message.capture_seq > cursor.capture_seq)) break
    seenSequences.add(message.capture_seq); last = message.capture_seq; previousTime = message.time_us
    const topic = message.topic.replace(/^\//, ''), data = replayObject(message.data)
    if (topic.startsWith('quote/')) { const value = quote(data, message.time_us, message.capture_seq, topic); if (value) { state.view.quotes[value.symbol] = value; state.view.quoteHistory.push(value) } }
    else if (topic.startsWith('market_trade/')) applyTrade(state, { ...data, symbol: symbolOf(data, topic) }, message, true)
    else if (topic === 'order') applyOrder(state, data, message)
    else if (topic === 'trade') applyTrade(state, data, message, false)
    else if (topic === 'command') applyCommand(state, data, message)
    else if (topic === 'account_observation') applyObservation(state, data, message)
    else if (topic === 'order_set') applyOrderSet(state, data, message)
    else if (topic === 'connection') applyConnection(state, data, message)
    else if (topic === 'execution') {
      const execution = text(data.execution_id)
      if (execution) {
        const previous = state.executions[execution] ?? {}
        const bySymbol = { ...replayObject(previous.by_symbol) }
        for (const [symbol, value] of Object.entries(replayObject(data.by_symbol))) bySymbol[symbol] = mergeFields(replayObject(bySymbol[symbol]), replayObject(value))
        const symbol = text(data.symbol)
        if (symbol) bySymbol[symbol] = mergeFields(replayObject(bySymbol[symbol]), data)
        state.executions[execution] = { ...mergeFields(previous, data), by_symbol: bySymbol,
          start_observed: previous.start_observed === true || ['start', 'started'].includes(text(data.event) ?? '')
            || ['start', 'started'].includes(text(data.phase) ?? '') }
        const targets = state.view.targetsByExecution[execution] ??= {}
        for (const [symbol, value] of Object.entries(bySymbol)) {
          const target = number(known(replayObject(value), 'target_quantity'))
          if (target != null) targets[symbol] = target
        }
      }
      const symbol = text(data.symbol), target = number(known(data, 'target_quantity'))
      if (symbol && target != null) state.view.targets[symbol] = target
      for (const [key, value] of Object.entries(replayObject(data.by_symbol))) {
        const target = number(replayObject(value).target_quantity)
        if (target != null) state.view.targets[key] = target
      }
    }
    state.view.capture_seq = message.capture_seq
    if (state.view.orderHistory.length > 20_000 || state.view.quoteHistory.length > 20_000) throw new Error('当前窗口事件过多，请选择更短时间范围')
  }
  state.view.time_us = cursor.time_us
  for (const [symbol, value] of Object.entries(state.view.quotes)) {
    if (value.valid_until_us == null ? cursor.time_us > value.time_us : cursor.time_us >= value.valid_until_us) state.view.quotes[symbol] = { ...value, book_valid: false }
  }
  for (const gap of window.gaps) {
    if (cursor.time_us >= gap.start_us && cursor.time_us < gap.end_us) {
      warn(state, gap.reason ?? '当前处于录制缺口')
      if (!gap.topic || gap.topic.includes('quote') || gap.topic === 'depth') {
        for (const [symbol, value] of Object.entries(state.view.quotes)) if (!gap.symbol || gap.symbol === symbol) state.view.quotes[symbol] = { ...value, book_valid: false }
      }
    }
  }
  rebuildTotals(state)
  derivePositions(state)
  state.view.ownTrades.sort((left, right) => left.capture_seq - right.capture_seq)
  state.view.marketTrades.sort((left, right) => left.capture_seq - right.capture_seq)
  return state.view
}
