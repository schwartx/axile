import type { AccountUpdatePayload } from '@/lib/api/accounts'
import type { Account } from '@/types/api'

type LeverageSettings = Pick<Account, 'long_leverage' | 'short_leverage' | 'weight_precision'>

/** 只发送用户改动的字段，保留编辑期间其他来源写入的配置。 */
export function leverageSettingsPatch(baseline: LeverageSettings, draft: LeverageSettings): AccountUpdatePayload {
  const patch: AccountUpdatePayload = {}
  if (draft.long_leverage != null && draft.long_leverage !== baseline.long_leverage) {
    patch.long_leverage = draft.long_leverage
  }
  if (draft.short_leverage != null && draft.short_leverage !== baseline.short_leverage) {
    patch.short_leverage = draft.short_leverage
  }
  if (draft.weight_precision !== baseline.weight_precision) {
    patch.weight_precision = draft.weight_precision
  }
  return patch
}
