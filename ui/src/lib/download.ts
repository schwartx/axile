/** 将已经接收的文件交给浏览器，不宣称用户已经保存到磁盘。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  try {
    document.body.append(link)
    link.click()
  } catch (error) {
    URL.revokeObjectURL(url)
    throw error
  } finally {
    link.remove()
  }
  // 浏览器需要先接管 blob URL；下一轮立即回收可能打断下载，留出短暂交接时间。
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
