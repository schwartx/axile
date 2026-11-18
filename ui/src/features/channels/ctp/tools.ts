import { CommissionRateQuery } from '@/features/channels/ctp/CommissionRateQuery'
import { MarginRateQuery } from '@/features/channels/ctp/MarginRateQuery'
import type { AccountTool } from '@/features/tools/ToolsButton'

export const tools = [
  { value: 'margin', label: '保证金查询', panel: MarginRateQuery },
  { value: 'commission', label: '手续费查询', panel: CommissionRateQuery },
] satisfies AccountTool[]
