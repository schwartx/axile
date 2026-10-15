/**
 * 与 theme.css ``ink-rewrite-*`` 时长对齐（ms）。
 *
 * 比工具台默认 130–160ms 慢一档：贴近电影里墨褪再显，而不是控件闪一下。
 * label（按钮）略短；prose（状态句）更慢、更「写完一行」。
 */
export const INK_REWRITE_MS_LABEL = 420
export const INK_REWRITE_MS_PROSE = 620

export type InkTone = 'label' | 'prose'

/** 按笔法取退场清理时长。 */
export function inkRewriteMs(tone: InkTone): number {
  return tone === 'prose' ? INK_REWRITE_MS_PROSE : INK_REWRITE_MS_LABEL
}

export type InkPair = {
  cur: string
  /** cur 层的「墨」（颜色/流光类）：随句快照，进层穿新墨。 */
  curCls: string
  prev: string | null
  /** prev 层的旧墨：旧句以旧色退场，不被新态瞬时染色。 */
  prevCls: string | null
  gen: number
}

/**
 * 计算下一帧日记对（纯函数，供测试与组件共用）。
 *
 * 颜色/流光是句子身份的一部分，随句快照进对：出层穿 ``prevCls``（旧墨以旧色褪），
 * 进层穿 ``curCls``（新墨以新色显）——槽外父级若瞬时改色，只影响当前态元素，
 * 染不到正在退场的旧句。
 *
 * Parameters
 * ----------
 * pair : InkPair
 *     当前对。
 * text : string
 *     目标文案。
 * cls : string
 *     目标文案的墨（颜色/流光类名）。
 * first : boolean
 *     是否首帧（首帧只就位、不退场）。
 *
 * Returns
 * -------
 * InkPair
 *     更新后的对；文案未变时墨就地换（不重写），全同则返回原引用。
 */
export function nextInkPair(pair: InkPair, text: string, cls: string, first: boolean): InkPair {
  if (first) return { cur: text, curCls: cls, prev: null, prevCls: null, gen: 0 }
  if (pair.cur === text) {
    // 文案未变、墨变（罕：同句换态类）：就地换墨即可，别触发重写。
    return pair.curCls === cls ? pair : { ...pair, curCls: cls }
  }
  return { cur: text, curCls: cls, prev: pair.cur, prevCls: pair.curCls, gen: pair.gen + 1 }
}

