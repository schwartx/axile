import { dict, number } from '@/features/account/executionJournal'
import { isDegradedSnapshotSource } from '@/features/account/accountSnapshot'
import type { ChannelCapability, ExecutionArtifact } from '@/types/api'

export function snapshotPositions(artifacts: ExecutionArtifact[], type: string) {
  const content = dict(artifacts.find(a => a.artifact_type === type)?.content)
  const assets = dict(content.account_assets)
  const source = content.source ?? assets.source
  if (isDegradedSnapshotSource(source) || !Array.isArray(assets.positions)) return null
  const rows = assets.positions.map(raw => { const p = dict(raw); return { symbol: String(p.symbol ?? ''), volume: number(p.volume), direction: String(p.direction ?? '') } })
  return rows.every(p => p.symbol && p.volume != null) ? rows.filter(p => p.volume !== 0) : null
}

export function quantityUnit(units: ChannelCapability['units'] | undefined, _symbol: string, _currency: string) {
  return units?.quantity_kind === 'base_asset' ? '' : units?.quantity_label ?? ''
}
