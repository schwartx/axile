import { useEffect } from 'react'
import { create } from 'zustand'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'

import { getChannelCapabilities } from '@/lib/api/system'
import type { ChannelCapability, TradeChannel } from '@/types/api'

interface ChannelCatalogState {
  channels: ChannelCapability[] | null
  loading: boolean
  error: Error | null
  refresh: () => Promise<void>
}

let catalogController: AbortController | null = null

/** 后端运行时注册的交易渠道目录。 */
export const useChannelCatalogStore = create<ChannelCatalogState>((set) => ({
  channels: null,
  loading: false,
  error: null,
  refresh: async () => {
    catalogController?.abort()
    const controller = new AbortController()
    catalogController = controller
    set({ loading: true, error: null })
    try {
      const channels = await getChannelCapabilities(controller.signal)
      if (!controller.signal.aborted) set({ channels, loading: false })
    } catch (error) {
      if (!controller.signal.aborted) {
        set({ loading: false, error: error instanceof Error ? error : new Error(String(error)) })
      }
    }
  },
}))

/** 同步读取指定渠道描述；未加载或未注册时返回 ``undefined``。 */
export function getChannelDescriptor(channel: TradeChannel | null | undefined): ChannelCapability | undefined {
  if (!channel) return undefined
  return useChannelCatalogStore.getState().channels?.find((item) => item.channel === channel)
}

/** 订阅指定渠道描述。 */
export function useChannelDescriptor(channel: TradeChannel | null | undefined): ChannelCapability | undefined {
  return useChannelCatalogStore((state) => state.channels?.find((item) => item.channel === channel))
}

/** 返回指定市场首个可用渠道；无兼容渠道时返回 ``undefined``。 */
export function getChannelForMarket(market: string | null | undefined): TradeChannel | undefined {
  if (!market) return undefined
  return useChannelCatalogStore
    .getState()
    .channels?.find(
      (channel) =>
        channel.available &&
        (channel.market === market || channel.portfolio.market_label === market),
    )?.channel
}

/** 在应用根加载一次渠道目录，并使目录更新触发页面重渲染。 */
export function useChannelCatalogSync(): void {
  const channels = useChannelCatalogStore((state) => state.channels)
  const refresh = useChannelCatalogStore((state) => state.refresh)
  useReadRecovery(refresh)
  useEffect(() => {
    if (channels === null) void refresh()
  }, [channels, refresh])
}
