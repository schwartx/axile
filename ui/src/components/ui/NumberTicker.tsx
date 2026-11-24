/** 实时数字滚动组件（收口缓动 / 时长 / 无障碍）。 */
import NumberFlow from '@number-flow/react'
import type { ComponentProps } from 'react'

/*
 * 缓动 / 时长镜像自 `ui/src/styles/theme.css`（动效真源）——NumberFlow 用 JS `EffectTiming`，
 * 无法直接读 CSS 变量，故此处以同值重述；改缓动时两处一并改，勿各写一套。
 *
 * - spin（数位滚动）：贴 `Segmented` 滑块的「快起慢落·落位感」缓动，让数字像里程表落位；
 *   刻意慢于工具台 200ms 档（与 `InkRewrite` 的 420–620ms 同精神：读作「落位」而非「闪切」）。
 * - transform（宽度 / 位移）：标准 200ms；数字变宽时下游兄弟顺势 reflow（不量宽、不叠第二套过渡）。
 * - opacity（进出字符淡入淡出）：标准 200ms。
 */
const SPIN_TIMING = { duration: 520, easing: 'cubic-bezier(0.32, 0.72, 0, 1)' }
const TRANSFORM_TIMING = { duration: 200, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const OPACITY_TIMING = { duration: 200, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const SCRUB_SPIN_TIMING = { ...SPIN_TIMING, duration: 160 }
const INSTANT_TIMING = { duration: 0 }

/** `NumberTicker` props：NumberFlow 全量 props 去掉由本组件收口的 timing 三项。 */
type NumberTickerProps = Omit<
  ComponentProps<typeof NumberFlow>,
  'spinTiming' | 'transformTiming' | 'opacityTiming'
> & { scrub?: boolean }

/**
 * 实时数字滚动组件。

 * 仅用于「随时间真变的实时值」（仪表盘生命体征、执行实时读数），以里程表式落位传达
 * 「同一数字、新值」的信息连续性。配置态 / 编辑态 / 事后取证 / 图表 SVG 数字**不应**使用
 * （见设计语言：克制、安静即好，逐个滚动即圣诞树）。

 * 三条设计地板由 NumberFlow 原生保证，无需本组件额外门控：首帧不演（仅 `value` 变化才动）、
 * 事件门控（值不变即惰性、空闲不挂）、`respectMotionPreference` 默认真（`reduce` 下直接落定值）。

 * Parameters
 * ----------
 * value : number
 *     当前数值；变化时触发滚动，涨滚上、跌滚下（`trend` 默认）。
 * format : Intl.NumberFormatOptions, optional
 *     数字格式（小数位、千分位分组等）。
 * locales : Intl.LocalesArgument, optional
 *     语言环境，默认 ``'zh-CN'``。
 * scrub : boolean, optional
 *     用于光标扫读，数位快速滚动，字符和宽度立即更新。
 * 其余
 *     透传 NumberFlow（``prefix`` / ``suffix`` / ``className`` / ``trend`` 等）。
 */
export function NumberTicker({ locales = 'zh-CN', scrub = false, ...props }: NumberTickerProps) {
  return (
    <NumberFlow
      locales={locales}
      spinTiming={scrub ? SCRUB_SPIN_TIMING : SPIN_TIMING}
      transformTiming={scrub ? INSTANT_TIMING : TRANSFORM_TIMING}
      opacityTiming={scrub ? INSTANT_TIMING : OPACITY_TIMING}
      {...props}
    />
  )
}
