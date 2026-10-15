/** 首启初始化向导接口。 */
import { apiGet, apiSend } from '@/lib/api/client'

/** 初始化状态中可安全返回给浏览器的配置摘要。 */
export interface InitStatusValues {
  sqlalchemy_database_configured: boolean
  exe_err_feishu_configured: boolean
  system_execution_notification_mode: 'default' | 'function'
  system_execution_notification_code: string
  environment: string
  app_log_dir: string
  axile_log_rotation: string
  algorithm_modules: string[]
  algorithm_directories: string[]
}

/** 向导提交的写入载荷；凭证只存在于用户当前输入中。 */
export interface InitValues {
  sqlalchemy_database_uri: string
  /** 执行错误告警飞书机器人 key（系统级，区别于账户各自的 `feishu_key`）；空串表示不推送。 */
  exe_err_feishu_key: string
  exe_err_feishu_configured?: boolean
  system_execution_notification_mode: 'default' | 'function'
  system_execution_notification_code: string
  environment: string
  app_log_dir: string
  axile_log_rotation: string
  algorithm_modules: string[]
  algorithm_directories: string[]
}

/** 初始化就绪状态与预填值。 */
export interface InitStatus {
  configured: boolean
  environment: string
  values: InitStatusValues
}

/** 连通性测试 / 保存操作结果。 */
export interface TestResult {
  ok: boolean
  message: string
}

/**
 * 最近一次成功的 `/init/status` 预填值缓存。
 *
 * 启动时拉到的值与当前 `config.toml` 一致。缓存供系统配置页同步取用，免去二次
 * fetch 造成的加载闪屏；热更新配置成功后须同步维护这份缓存。
 */
let cachedInitValues: InitValues | null = null

/** 将安全状态摘要转换为可编辑草稿，凭证字段始终从空值开始。 */
export function initValuesFromStatus(values: InitStatusValues): InitValues {
  return {
    sqlalchemy_database_uri: '',
    exe_err_feishu_key: '',
    exe_err_feishu_configured: values.exe_err_feishu_configured,
    system_execution_notification_mode: values.system_execution_notification_mode ?? 'default',
    system_execution_notification_code: values.system_execution_notification_code ?? '',
    environment: values.environment,
    app_log_dir: values.app_log_dir,
    axile_log_rotation: values.axile_log_rotation,
    algorithm_modules: values.algorithm_modules,
    algorithm_directories: values.algorithm_directories,
  }
}

/** 查询初始化就绪状态与预填值。`signal` 用于配合轮询取消。 */
export function initStatus(signal?: AbortSignal): Promise<InitStatus> {
  return apiGet<InitStatus>('/init/status', signal).then((status) => {
    cachedInitValues = initValuesFromStatus(status.values)
    return status
  })
}

/** 同步读取上一次成功拉取的预填值；从未拉取成功时返回 `null`。 */
export function peekInitValues(): InitValues | null {
  return cachedInitValues
}

/** 测试数据库连通性。 */
export function testDb(uri: string): Promise<TestResult> {
  return apiSend<TestResult>('POST', '/init/test-db', { uri })
}

/** 测试执行告警飞书机器人连通性（向其推送一张联通测试卡片）。 */
export function testFeishu(key: string | null): Promise<TestResult> {
  return apiSend<TestResult>('POST', '/init/test-feishu', { key })
}

/** 保存系统级执行错误告警配置；成功后当前服务进程立即使用新值。 */
export function saveExecutionAlert(exeErrFeishuKey: string | null, mode: 'default' | 'function', code: string): Promise<TestResult> {
  return apiSend<TestResult>('PATCH', '/init/execution-alert', {
    exe_err_feishu_key: exeErrFeishuKey,
    system_execution_notification_mode: mode,
    system_execution_notification_code: code,
  }).then((result) => {
    if (result.ok && cachedInitValues) {
      cachedInitValues = { ...cachedInitValues, exe_err_feishu_key: '', exe_err_feishu_configured: exeErrFeishuKey === null ? cachedInitValues.exe_err_feishu_configured : Boolean(exeErrFeishuKey), system_execution_notification_mode: mode, system_execution_notification_code: code }
    }
    return result
  })
}

/** 用样例异常运行系统通知函数草稿。 */
export function testSystemNotificationFunction(code: string, key: string | null = null, eventType: SystemNotificationEvent = 'execution_error'): Promise<SystemNotificationTestResult> {
  return apiSend<SystemNotificationTestResult>('POST', '/init/execution-alert/function/test', { code, key, event_type: eventType })
}

/** 保存初始化配置；成功后后端将自退出并由 supervisor 拉起重启。 */
export function saveInit(values: ReturnType<typeof initSavePayload>): Promise<TestResult> {
  return apiSend<TestResult>('POST', '/init/save', values)
}

/** 高级设置只提交新输入的数据库地址，告警由独立表单维护。 */
export function initSavePayload(values: InitValues, isEdit: boolean) {
  const { exe_err_feishu_configured: _configured, ...editable } = values
  return {
    ...editable,
    sqlalchemy_database_uri: isEdit ? (values.sqlalchemy_database_uri.trim() || undefined) : values.sqlalchemy_database_uri,
    exe_err_feishu_key: isEdit ? undefined : values.exe_err_feishu_key,
    system_execution_notification_mode: isEdit ? undefined : values.system_execution_notification_mode,
    system_execution_notification_code: isEdit ? undefined : values.system_execution_notification_code,
  }
}

export type SystemNotificationEvent = 'execution_error' | 'execution_timeout' | 'account_control.warning'
export interface SystemNotificationTestResult extends TestResult { error_line?: number | null; traceback?: string | null }
export interface SystemNotificationResult {
  finished_at: string | null
  mode: 'default' | 'function' | null
  event_type: string | null
  execution_id: string | null
  account_id: number | null
  ok: boolean | null
  error: string | null
}
export function getDefaultSystemNotification(signal?: AbortSignal): Promise<string> {
  return apiGet<{ code: string }>('/init/execution-alert/function/default', signal).then((result) => result.code)
}
export function getSystemNotificationResult(signal?: AbortSignal): Promise<SystemNotificationResult> {
  return apiGet<SystemNotificationResult>('/init/execution-alert/result', signal)
}
