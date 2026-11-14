import type { HeatQuery, HeatResult } from './heatData'
import { analysisWorker } from '@/lib/analysisWorker'

export const replayHeatWorker = (worker: Worker, result: (query: HeatQuery, value: HeatResult) => void, error: (message: string) => void) => analysisWorker(worker, result, error, '历史深度计算失败，请重新打开回放')
