import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { RawReplayPlot } from './RawReplayPlot'
import type { RawBusiness } from './raw'

test('the renderer keeps at most 128 quote groups, eight levels per side, and 128 trade bins per source', () => {
  const quotes = Array.from({length:300},(_,i)=>({time_us:i,observed_us:i,until_us:i+1,capture_seq:i+1,bids:Array.from({length:20},()=>[100,1]),asks:Array.from({length:20},()=>[102,1])}))
  const bins = Array.from({length:300},(_,index)=>({index,count:1,quantity:1,vwap:101,unknown:0}))
  const subject = {data:{viewport:{quotes},own:{timeline:bins},market:{timeline:bins}}} as unknown as RawBusiness
  const html=renderToStaticMarkup(createElement(RawReplayPlot,{kind:'interval',subjects:[subject],start:0,end:1000,onSeek:()=>{}}))
  expect((html.match(/<rect /g)??[]).length).toBe(128*8*2)
  expect((html.match(/<circle /g)??[]).length).toBe(128*2)
  expect((html.match(/role="button"/g)??[]).length).toBe(128*3)
  expect(html).not.toContain('NaN')
})

test('order display limits do not grow with historical observations or identity pages', () => {
  const subjects=Array.from({length:100},(_,i)=>({key:`O${i}`,data:{history:Array.from({length:100},(_,j)=>({time_us:j,capture_seq:j+1,price:100,quantity:1}))}})) as unknown as RawBusiness[]
  const html=renderToStaticMarkup(createElement(RawReplayPlot,{kind:'order',subjects,start:0,end:100,onSeek:()=>{}}))
  expect((html.match(/<circle /g)??[]).length).toBe(8*16)
  expect(html).toContain('点之间不推断订单状态')
})
