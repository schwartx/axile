/** 证据包下载采用二进制接口，不从页面已有数据拼包。 */
import { ApiError, apiGetBlob } from '@/lib/api/client'
import { evidenceUrl, type EvidenceOptions, type EvidenceTarget } from '@/features/account/evidenceExport'

export async function getEvidencePackage(target: EvidenceTarget, options: EvidenceOptions, signal: AbortSignal): Promise<Blob> {
  const blob = await apiGetBlob(evidenceUrl(target, options), signal)
  if (blob.type.split(';')[0].trim().toLowerCase() !== 'application/zip' || blob.size === 0) {
    throw new ApiError(502, '服务返回的证据文件格式不正确，请重试。', { code: 'EVIDENCE_INVALID_RESPONSE' })
  }
  return blob
}
