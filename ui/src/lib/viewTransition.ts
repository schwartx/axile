/**
 * 页内状态切换的 View Transition 包装（L2/L3）与 panel-fade 入场闸门。
 *
 * 第一性原理：路由切换已由 `components/ui/nav.tsx` 统一走 View Transitions，页内的
 * 状态切换（如图表「每日/累计」、区间「30/90/全部」）同属「一次连续变化」，也应走同一
 * 套原生过渡，而非硬切。浏览器不支持时直接执行更新降级；``prefers-reduced-motion``
 * 由 `theme.css` 的 ``::view-transition`` 规则兜底关闭。
 *
 * 同页 Tab 内容区优先 ``panel-fade-in``（见 theme.css），勿与 Segmented 滑块抢 root VT。
 */
import { useEffect, useRef } from 'react'
import { flushSync } from 'react-dom'

/** 带 `startViewTransition` 能力的 document（渐进增强，避免 TS 报缺方法）。 */
type ViewTransitionDocument = Document & {
  startViewTransition?: (callback: (() => void) | { update: () => void; types: string[] }) => unknown
}

/**
 * 在 View Transition 中执行一次状态更新。

 * React 的 ``setState`` 默认异步提交，若不同步 flush，过渡回调结束时 DOM 尚未变化，
 * 新旧快照相同便动不起来；故用 ``flushSync`` 强制在回调内同步提交 DOM。

 * Parameters
 * ----------
 * update : () => void
 *     触发状态变更的回调（通常是一个或多个 ``setState``）。
 * type : string, optional
 *     页内过渡类型，用 CSS 限定参与区域；不支持时立即更新。

 * Notes
 * -----
 * 浏览器不支持 View Transitions API 时直接同步执行 ``update``，行为与硬切一致。
 */
export function withViewTransition(update: () => void, type?: string): void {
  const doc = document as ViewTransitionDocument
  if (typeof doc.startViewTransition !== 'function' || (type && (
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
    || !CSS.supports(`selector(:active-view-transition-type(${type}))`)
  ))) {
    update()
    return
  }
  const commit = () => flushSync(update)
  doc.startViewTransition(type ? { update: commit, types: [type] } : commit)
}

/**
 * 仅当 key 相对上次提交发生变化的那一帧返回 true，配合 ``key={k}`` 重挂播 ``panel-fade-in``。
 *
 * 对齐动效铁律「首帧就位，不入场表演」：首帧与挂载后的无关重渲染都返回 false，只有
 * key 真变（节点被重挂成新 DOM）的那次渲染才放行入场淡入。
 *
 * 为何不沿用「mount 后置 true」的 ref 闸门：渲染期读 ref 时，挂载后的任意无关重渲染
 * （如约 250ms 后到点的预览 loading）都会把动画类补挂到存续节点上，浏览器对新增
 * animation 类会从头重播——表现为每次打开页面都闪一下。比较「本次 key 与上次提交的
 * key」则只在新节点诞生的那一帧成立，存续节点永远拿不到这个类。
 *
 * Parameters
 * ----------
 * key : unknown
 *     与被动画节点 ``key`` 同源的身份值（tab、主锚身份、开关态等）。
 *
 * Returns
 * -------
 * boolean
 *     本帧是否应挂 ``panel-fade-in``。
 */
export function useRemountFade(key: unknown): boolean {
  const committed = useRef(key)
  const changed = committed.current !== key
  useEffect(() => {
    committed.current = key
  })
  return changed
}

/** 标准 200ms 布局过渡（目录宽/透明度）；reduce 时由 utility 关掉。 */
export const MOTION_LAYOUT =
  'duration-200 ease-[cubic-bezier(0.4,0,0.2,1)] motion-reduce:transition-none'
