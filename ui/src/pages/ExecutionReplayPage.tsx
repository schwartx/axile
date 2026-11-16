import { Fragment, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useParams } from 'react-router'
import { AccountPageTitle } from '@/features/account/pageHead'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Select } from '@/components/ui/Select'
import { RawReader } from '@/features/replay/rawReader'
import { RawReplayPlot } from '@/features/replay/RawReplayPlot'
import type { RawCursor, RawFrame, RawManifest, RawTarget } from '@/features/replay/raw'

const buttonClass = 'min-h-10 rounded-chip border border-line px-3 text-sm text-ink-2 hover:bg-fill disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-accent'
const time = (us: number) => new Date(us / 1000).toLocaleString('zh-CN', { hour12: false }) + `.${String(us % 1_000_000).padStart(6, '0')}`
const mib = (bytes: number) => (bytes / 1024 / 1024).toFixed(2)

function IntervalView({data}:{data:Record<string,unknown>}) {
  const own=data.own as Record<string,unknown>, market=data.market as Record<string,unknown>, book=data.book as Record<string,unknown>
  const metrics=[['stable_count','稳定身份数'],['weak_count','未解析弱身份数'],['known_quantity','已确认数量'],['known_amount','已确认金额'],['vwap','已确认部分 VWAP'],['unknown_count','未知身份数'],['unowned_count','归属不明数']] as const
  return <div className="mt-3 space-y-3 text-sm">
    <p className="text-ink-3">区间 [{time(Number(data.interval_start_us))}, {time(Number(data.interval_end_us))})；仅使用目标时点已知的字段。自有与市场分别汇总，未知部分不计为零。</p>
    <table className="w-full text-left tabular-nums"><caption className="text-left">区间成交</caption><thead><tr><th>指标</th><th>自有</th><th>市场</th></tr></thead><tbody>
      {metrics.map(([field,label])=><tr key={field}><th scope="row">{label}</th><td>{String(own[field]??'未知')}</td><td>{String(market[field]??'未知')}</td></tr>)}
      <tr><th scope="row">完整性</th><td>{own.complete?'完整':'不完整，仅已确认部分'}</td><td>{market.complete?'完整':'不完整，仅已确认部分'}</td></tr>
    </tbody></table>
    <dl className="grid grid-cols-2 gap-2">{[['spread_mean','平均价差'],['imbalance_mean','平均不平衡'],['spread_valid_us','价差有效时长（微秒）'],['imbalance_valid_us','不平衡有效时长（微秒）'],['query_us','查询时长（微秒）']].map(([field,label])=><Fragment key={field}><dt>{label}</dt><dd>{String(book[field]??'未知')}</dd></Fragment>)}<dt>报价覆盖</dt><dd>{book.complete?'完整':'含过期、缺口或未确认采集区间'}</dd></dl>
    {([['own',own,'自有'],['market',market,'市场']] as const).map(([key,source,label])=>{
      const grid=source.distribution as {origin_tick:number;width_ticks:number;bins:{index:number;count:number;quantity:number}[]}|undefined
      return grid&&<details key={key}><summary>{label}价格分布（价格 tick {String(data.price_tick??'未知')}）</summary><p>统一原点 {grid.origin_tick} tick；宽度 {grid.width_ticks} tick；最多 128 桶。</p><table className="w-full text-left"><thead><tr><th>桶序号</th><th>身份数</th><th>数量</th></tr></thead><tbody>{grid.bins.map(bin=><tr key={bin.index}><td>{bin.index}</td><td>{bin.count}</td><td>{bin.quantity}</td></tr>)}</tbody></table></details>
    })}
  </div>
}

export function ExecutionReplayPage() {
  const { id, executionId = '' } = useParams(), accountId = Number(id)
  const [manifest, setManifest] = useState<RawManifest | null>(null)
  const [recordingId, setRecording] = useState(''), [stream, setStream] = useState(0)
  const [symbol, setSymbol] = useState(''), [symbolDraft, setSymbolDraft] = useState('')
  const [target, setTarget] = useState(0), [frame, setFrame] = useState<RawFrame | null>(null)
  const [captureSeq, setCaptureSeq] = useState<number>()
  const [error, setError] = useState<Error | null>(null), [revision, setRevision] = useState(0)
  const [busy, setBusy] = useState(true), [playing, setPlaying] = useState(false)
  const [kind, setKind] = useState<NonNullable<RawTarget['business']>['kind']>('trade')
  const [cursor, setCursor] = useState<RawCursor>([0, 0])
  const [cursorGeneration,setCursorGeneration] = useState<string>()
  const [intervalStart, setIntervalStart] = useState<number | null>(null)
  const [priceTick, setPriceTick] = useState('0.01'), [distribution, setDistribution] = useState(false)
  const [showPlot, setShowPlot] = useState(false)
  const reader = useRef<RawReader | null>(null)
  const recording = manifest?.recordings.find(row => row.id === recordingId)
  const streams = recording?.streams.filter(row => row.topic.startsWith('/quote/')) ?? []
  const symbols = [...new Set([symbol,...(recording?.symbols??[]),...streams.map(s=>s.symbol)].filter(Boolean))]

  useEffect(() => {
    let active = true
    setBusy(true); setError(null); setManifest(null); setFrame(null); setPlaying(false); setCursor([0, 0]); setCaptureSeq(undefined)
    const instance = new RawReader(executionId, value => {
      if (active) { setFrame(value); setBusy(false) }
    }, cause => { if (active) { setError(cause); setBusy(false); setPlaying(false) } })
    reader.current = instance
    void instance.manifest().then(value => {
      if (!active) return
      if (value.account_id !== accountId || value.execution_id !== executionId) throw Error('这次执行不属于当前账户')
      setManifest(value)
      const first = value.recordings.find(row => row.start_us !== null)
      setRecording(first?.id ?? '')
      const initialSymbol=first?.streams.find(row=>row.topic.startsWith('/quote/'))?.symbol??first?.symbols?.[0]??first?.streams.find(row=>row.symbol)?.symbol??''
      setSymbol(initialSymbol)
      setStream(first?.streams.find(row => row.topic.startsWith('/quote/')&&row.symbol===initialSymbol)?.id ?? 0)
      setTarget(first?.start_us ?? 0)
      if (!first) setBusy(false)
    }).catch(cause => { if (active) { setError(cause instanceof Error ? cause : Error(String(cause))); setBusy(false) } })
    return () => { active = false; instance.close(); reader.current = null }
  }, [accountId, executionId, revision])

  useLayoutEffect(() => {
    if (!recording || !manifest || recording.start_us === null) return
    setBusy(true); setError(null)
    reader.current?.seek({ recording, stream, symbol, time_us: target, capture_seq: captureSeq, generation: manifest.index_generation, business: { kind, after: cursor, cursor_generation:cursorGeneration,
      ...(kind === 'interval' ? { interval: { start_us: intervalStart ?? recording.start_us, end_us: target + 1, price_tick: Number(priceTick), distribution, viewport: showPlot } } : {}) } })
  }, [recording, stream, symbol, target, captureSeq, manifest, kind, cursor, cursorGeneration, intervalStart, priceTick, distribution, showPlot])

  useEffect(() => {
    if (!playing || busy || !recording || document.hidden) return
    const timer = setTimeout(() => {
      const next = Math.min(recording.watermark, target + 1_000_000)
      if (next === target) setPlaying(false)
      else { setCursor([0, 0]); setCaptureSeq(undefined); setTarget(next) }
    }, 250)
    return () => clearTimeout(timer)
  }, [playing, busy, recording, target])
  useEffect(() => {
    const hidden = () => { if (document.hidden) setPlaying(false) }
    document.addEventListener('visibilitychange', hidden)
    return () => document.removeEventListener('visibilitychange', hidden)
  }, [])

  const quote = frame?.quote
  const valid = frame?.quote_valid === true
  const sides = ['bids', 'asks'] as const
  return <div className="flex min-w-0 flex-col gap-5 p-4 md:p-6">
    <AccountPageTitle accountId={accountId} page="执行回放" flip={false} back={{ to: `/accounts/${accountId}/executions/${executionId}`, label: '返回执行详情' }} />
    <p className="text-sm text-ink-3">按观察时间查看盘口、最近原始证据及账户业务观察。成交按原始身份合并；业务对象每页最多 8 项。</p>
    {error && <ErrorNotice intent="read" title="回放读取失败" error={error} onRetry={() => setRevision(value => value + 1)} />}
    {manifest && manifest.recordings.length === 0 && <p className="text-sm text-ink-3">本次执行没有新格式录制。</p>}
    {recording && <>
      <div className="flex flex-wrap items-center gap-3">
        <Select ariaLabel="选择录制段" value={recordingId} options={manifest!.recordings.map(row => ({ value: row.id, label: row.id }))} onChange={value => {
          const next = manifest!.recordings.find(row => row.id === value)!
          setPlaying(false); setFrame(null); setCursor([0, 0]); setCaptureSeq(undefined); setIntervalStart(null); setRecording(value); setTarget(next.start_us ?? 0)
          const selected=next.streams.find(row=>row.topic.startsWith('/quote/'))?.symbol??next.symbols?.[0]??next.streams.find(row=>row.symbol)?.symbol??''
          setSymbol(selected);setStream(next.streams.find(row => row.topic.startsWith('/quote/')&&row.symbol===selected)?.id ?? 0)
        }} />
        {symbols.length > 0 && <Select ariaLabel="选择回放品种" value={symbol} options={symbols.map(value=>({value,label:value}))} onChange={value => { setPlaying(false); setFrame(null);setCursor([0,0]);setSymbol(value);setStream(streams.find(s=>s.symbol===value)?.id??0) }} />}
        {recording.symbols_more&&<input aria-label="完整品种代码" placeholder="输入完整品种代码并按回车" className="rounded border border-line p-2 text-sm" maxLength={1024} value={symbolDraft} onChange={e=>setSymbolDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&symbolDraft){setPlaying(false);setFrame(null);setCursor([0,0]);setSymbol(symbolDraft);setStream(streams.find(s=>s.symbol===symbolDraft)?.id??0)}}} />}
        <button className={buttonClass} disabled={Boolean(error) || kind === 'interval'} onClick={() => setPlaying(value => !value)}>{playing ? '暂停' : '播放'}</button>
        {(['previous','next'] as const).map(direction => <button key={direction} className={buttonClass} disabled={busy || !frame?.[direction]} onClick={() => {
          const event = frame![direction]!
          setPlaying(false); setCursor([0,0]); setTarget(event.time); setCaptureSeq(event.seq)
        }}>{direction === 'previous' ? '上一事件' : '下一事件'}</button>)}
        <button className={buttonClass} onClick={() => { setPlaying(false); setCursor([0, 0]); setCaptureSeq(undefined); setTarget(recording.watermark) }}>跳到末尾</button>
      </div>
      <label className="flex flex-col gap-2 text-sm text-ink-2">目标观察时间：{time(target)}
        <input aria-label="回放时间" type="range" min={recording.start_us ?? 0} max={recording.watermark} step={1} value={target}
          onChange={event => { setPlaying(false); setCursor([0, 0]); setCaptureSeq(undefined); setTarget(Number(event.target.value)) }} />
      </label>
      <p role="status" data-target-us={frame?.target_us} data-busy={busy} className="text-sm text-ink-3">{busy ? '计算中；下方保留上次已完成的时点。' : frame ? `已完成：${time(frame.target_us)}；观察序号 ${frame.target_seq}；盘口观察序号 ${quote?.capture_seq ?? '未知'}` : '尚无完成结果'}</p>
      <div className="rounded-card border border-line p-3">
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <Select<NonNullable<RawTarget['business']>['kind']> ariaLabel="业务观察类型" value={kind} options={[
              { value: 'trade', label: '自有成交（稳定身份）' }, { value: 'weak_trade', label: '自有成交（弱身份）' },
              { value: 'command', label: '命令观察' }, { value: 'order', label: '订单推导' }, { value: 'position', label: '持仓推导' }, { value: 'statistics', label: '执行统计（当前品种）' },
              { value: 'interval', label: '完整区间分析（当前品种）' },
            ]} onChange={value => { setPlaying(false); setFrame(null); setKind(value); setCursor([0, 0]) }} />
            <button className={buttonClass} disabled={busy || cursor[0] === 0} onClick={() => { setPlaying(false); setCursor([0, 0]) }}>首页</button>
            <button className={buttonClass} disabled={busy || !frame?.business?.next || frame?.business.kind !== kind} onClick={() => { setPlaying(false); setCursor(frame!.business!.next!);setCursorGeneration(frame!.index_generation) }}>下一页</button>
            {(kind==='interval'||kind==='order')&&<label className="text-sm"><input aria-label="显示历史图形" type="checkbox" checked={showPlot} onChange={event=>{setPlaying(false);setShowPlot(event.target.checked)}} /> 历史图形</label>}
          </div>
          {kind === 'interval' && <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
            <label>区间起点（观察时间，微秒）<input aria-label="区间起点" className="ml-2 rounded border border-line p-2" type="number" min={recording.start_us ?? 0} max={target} step={1} value={intervalStart ?? recording.start_us ?? 0} onChange={event => setIntervalStart(Number(event.target.value))} /></label>
            <label>最小价格变动<input aria-label="最小价格变动" className="ml-2 w-28 rounded border border-line p-2" type="number" min="0" step="any" value={priceTick} onChange={event => setPriceTick(event.target.value)} /></label>
            <label><input aria-label="两遍价格分布" type="checkbox" checked={distribution} onChange={event => setDistribution(event.target.checked)} /> 两遍价格分布（最多 128 桶）</label>
            {showPlot&&<div className="flex gap-2">{[.5,2].map(scale=><button key={scale} className={buttonClass} disabled={busy} onClick={()=>{const span=target+1-(intervalStart??recording.start_us??0);setIntervalStart(Math.max(recording.start_us??0,target+1-Math.max(1,Math.round(span*scale))))}}>{scale<1?'放大视口':'缩小视口'}</button>)}{[-1,1].map(direction=><button key={direction} className={buttonClass} disabled={busy} onClick={()=>{
              const span=target+1-(intervalStart??recording.start_us??0),end=Math.max((recording.start_us??0)+span,Math.min(recording.watermark+1,target+1+direction*Math.max(1,Math.floor(span/2))))
              setIntervalStart(end-span);setTarget(end-1);setCaptureSeq(undefined)
            }}>{direction<0?'向前平移':'向后平移'}</button>)}</div>}
            <p className="text-ink-3">终点为目标观察时间之后 1 微秒；区间左闭右开。完整区间分析按需运行，暂停自动播放。</p>
          </div>}
      </div>
      {frame && <>
        {frame.cost_estimated && <p className="text-sm text-warn">读取服务恢复后，缺失回执的尾部成本按预留上界结算；累计成本含估计值。</p>}
        <p className="text-sm text-ink-3">{valid ? typeof quote?.data.valid_until_us === 'number' ? '原始盘口在有效期内' : '原始盘口在该观察时点可用（有效期限未知）' : '盘口有效性未知或已过期'}；显示最多 20 档原始观察。未观察到不表示为零。</p>
        <div className="grid gap-4 md:grid-cols-2">
          {sides.map(side => <div key={side} className="overflow-x-auto rounded-card border border-line p-3">
            <table className="w-full text-left text-sm tabular-nums"><caption className="mb-2 text-left text-ink-2">{side === 'bids' ? '买盘' : '卖盘'}</caption>
              <thead><tr className="text-ink-3"><th scope="col">价格</th><th scope="col">数量</th></tr></thead>
              <tbody>{(Array.isArray(quote?.data[side]) ? quote.data[side] as unknown[] : []).slice(0, 20).map((row, i) => {
                const item = row && typeof row === 'object' ? row as Record<string, unknown> : {}
                const values = Array.isArray(row) ? row : [item.price, item.quantity]
                return <tr key={i}><td>{String(values[0] ?? '未知')}</td><td>{String(values[1] ?? '未知')}</td></tr>
              })}</tbody>
            </table>
          </div>)}
        </div>
        <div className="rounded-card border border-line p-3">
          <p className="mb-3 text-sm text-ink-3">范围是本账户此录制段截至目标时点。持仓保留各字段的原始基线与覆盖证明；证据不足显示未知。统计按当前执行、所选品种及录制开始至目标时点汇总，归属不明或弱身份会影响完整性。</p>
          {showPlot&&frame.business&&(frame.business.kind==='interval'||frame.business.kind==='order')&&<RawReplayPlot kind={frame.business.kind} subjects={frame.business.subjects} start={frame.business.kind==='interval'?Number(frame.business.subjects[0]?.data.interval_start_us):recording.start_us??0} end={frame.business.kind==='interval'?Number(frame.business.subjects[0]?.data.interval_end_us):frame.target_us+1} onSeek={(us,seq)=>{
            setPlaying(false);setTarget(us);setCaptureSeq(seq)
            if(kind==='interval'){setKind('trade');setCursor([0,0])}
          }} />}
          <div className="overflow-x-auto"><table className="w-full text-left text-sm">
            <caption className="mb-2 text-left text-ink-2">已完成时点的业务结果</caption>
            <thead><tr className="text-ink-3"><th scope="col">身份</th><th scope="col">品种</th><th scope="col">最近观察</th><th scope="col">数量</th><th scope="col">价格 / VWAP</th><th scope="col">执行归属</th><th scope="col">证据状态</th></tr></thead>
            <tbody>{frame.business?.subjects.map(subject => <tr key={subject.id}>
              <td className="max-w-64 break-all">{subject.key}</td><td>{String(subject.data.symbol ?? '未知')}</td><td>{subject.observed_seq || '未知'}</td>
              <td>{String((subject.kind === 'statistics' ? subject.data.known_quantity : subject.data.quantity) ?? '未知')}{subject.kind === 'position' && <span className="block text-xs text-ink-3">观察基线：{String(subject.baseline?.quantity ?? '未知')}</span>}</td><td>{String((subject.kind === 'statistics' ? subject.data.vwap : subject.data.price) ?? '未知')}</td><td className="max-w-48 break-all">{String((subject.kind === 'statistics' ? subject.key : subject.data.execution_id) ?? '未知')}</td>
              <td className={subject.conflict || subject.owner_conflict ? 'text-warn' : 'text-ink-2'}>{subject.conflict || subject.owner_conflict ? '存在冲突' : subject.kind === 'statistics' ? subject.data.complete ? '完整统计' : '仅已确认部分' : subject.kind === 'position' ? '按字段检查覆盖证明' : subject.kind === 'order' ? '订单推导' : subject.resolved_seq ? `已于 ${subject.resolved_seq} 解析` : subject.kind.includes('trade') && !subject.valid ? '数量或金额未知' : subject.kind === 'weak_trade' ? '弱身份' : '原始观察'}</td>
            </tr>)}</tbody>
          </table></div>
          {frame.business?.kind === 'statistics' && frame.business.subjects.map(subject => <dl key={subject.key} className="mt-3 grid grid-cols-2 gap-2 text-sm">
            <dt>品种</dt><dd>{String(subject.data.symbol ?? '未知')}</dd><dt>已确认成交量</dt><dd>{String(subject.data.known_quantity ?? '未知')}</dd><dt>已确认成交额</dt><dd>{String(subject.data.known_amount ?? '未知')}</dd>
            <dt>已确认部分 VWAP</dt><dd>{String(subject.data.vwap ?? '未知')}</dd><dt>稳定 / 弱身份数</dt><dd>{String(subject.data.stable_count)} / {String(subject.data.weak_count)}</dd><dt>未知 / 归属不明数</dt><dd>{String(subject.data.unknown_count)} / {String(subject.data.unowned_count)}</dd>
            <dt>统计完整性</dt><dd className={subject.data.complete ? 'text-ink-2' : 'text-warn'}>{subject.data.complete ? '完整' : '不完整，仅展示已确认部分'}</dd>
          </dl>)}
          {frame.business?.kind === 'interval' && frame.business.subjects.map(subject => <IntervalView key={subject.key} data={subject.data} />)}
          {frame.business?.kind === 'position' && frame.business.subjects.map(subject => <div key={subject.key} className="mt-3 text-sm">
            <p className="break-all text-ink-2">{subject.key}</p>
            <dl className="mt-1 grid grid-cols-2 gap-1 text-ink-3">{[['quantity', '总持仓'], ['today_quantity', '今仓'], ['yesterday_quantity', '昨仓'], ['available_quantity', '可用'], ['frozen_quantity', '冻结']].map(([field, label]) => <Fragment key={field}><dt>{label}</dt><dd>{String(subject.data[field] ?? '未知')}（观察 {String(subject.baseline?.[field] ?? '未知')}）</dd></Fragment>)}</dl>
          </div>)}
          {frame.business?.kind === 'order' && frame.business.subjects.map(subject => <p key={subject.key} className="mt-2 break-all text-sm text-ink-3">{subject.key}：已成交 {String(subject.data.filled_quantity ?? '未知')}；剩余 {String(subject.data.remaining_quantity ?? '未知')}；{String(subject.data.confirmation_state ?? '未知')}；撤单{subject.data.cancel_pending ? '等待确认' : '无待确认记录'}</p>)}
          {frame.business?.subjects.length === 0 && <p className="mt-2 text-sm text-ink-3">此时点没有这类已观察对象。</p>}
        </div>
        <div className="overflow-x-auto rounded-card border border-line p-3"><table className="w-full text-left text-sm">
          <caption className="mb-2 text-left text-ink-2">目标时点及此前的最近原始证据</caption>
          <thead><tr className="text-ink-3"><th scope="col">序号</th><th scope="col">观察时间</th><th scope="col">主题</th><th scope="col">品种 / 身份</th></tr></thead>
          <tbody>{frame.evidence.map(event => <tr key={event.capture_seq}><td>{event.capture_seq}</td><td className="whitespace-nowrap">{time(event.time_us)}</td><td>{event.topic}</td><td>{String(event.data.symbol ?? '')} {String(event.data.trade_key ?? event.data.order_id ?? event.data.execution_id ?? '')}</td></tr>)}</tbody>
        </table></div>
        <p className="text-xs text-ink-3">本次原始块读取 {mib(frame.read_bytes)} MiB；会话累计读取及索引响应预算 {mib(frame.cumulative_bytes)} MiB；已完成根任务 {frame.roots}；WASM 线性内存 {mib(frame.wasm_bytes)} MiB；压缩缓存 {mib(frame.cache_bytes)} MiB。</p>
      </>}
    </>}
  </div>
}
