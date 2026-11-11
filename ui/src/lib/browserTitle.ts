/** 与 index.html 首屏规则一致；自定义标题完整替换默认标题。 */
export function browserTitle(title: string, location: Pick<Location, 'hostname' | 'host' | 'port'>): string {
  const host = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)
    ? '本地' + (location.port ? ':' + location.port : '')
    : location.host
  return title.trim() || `axile(${host})`
}
