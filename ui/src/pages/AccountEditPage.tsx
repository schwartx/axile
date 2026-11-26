/**
 * 账户浅配置页 /accounts/:id/edit/*。
 *
 * 基本信息、组合执行各走可直达子路由；连接设置、定时、算法、流控
 * 仍由各自的完整编辑器承载。保存保持最小 PATCH + 底栏变更摘要；保存与取消都不离开本页。
 */

import { SIGNAL_CHANNEL } from '@/features/signal/signalData'
import { extractFeishuKey } from '@/features/account/feishuUpdate'
import { FeishuWebhookInput } from '@/features/account/FeishuWebhookInput'
import { basicNotificationPatch, basicNotificationStatus } from '@/features/account/basicNotification'
import { NOTIFICATION_STATUS_LABEL } from '@/features/account/notificationStatus'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { Link } from '@/components/ui/nav'
import { useState } from 'react'
import { Check, ExternalLink } from 'lucide-react'
import { Navigate, useOutletContext, useParams } from 'react-router'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { PortfolioChoiceList } from '@/features/portfolio/PortfolioChoiceList'
import { AccountPageTitle } from '@/features/account/pageHead'
import { getDefaultAccountNotification, testAccountFeishu, updateAccount, type AccountFeishuTestResult, type AccountUpdatePayload } from '@/lib/api/accounts'
import type { AccountEditContext } from '@/features/account/AccountEditGuard'
import { useDomainStore } from '@/stores/domain'
import { useChannelCatalogStore, useChannelDescriptor } from '@/stores/channels'
import { useToastStore } from '@/stores/ui'
import {
  TEXT,
  EditLoading,
  EditSaveBar,
  Row,
  Section,
  Toggle,
} from '@/features/account/editUi'
import type { Account, PortfolioLite } from '@/types/api'

/** 总览草稿：不含定时 / 算法（各在子页独立保存）。 */
interface Draft {
  name: string
  remark: string
  notificationCode: string | null
  clearFeishu: boolean
  feishu: string
  portfolioId: number | null
  writeEmpty: boolean
}

function draftOf(acc: Account): Draft {
  return {
    name: acc.name,
    remark: acc.remark ?? '',
    notificationCode: acc.execution_notification_code,
    clearFeishu: false,
    feishu: '',
    portfolioId: acc.portfolio_id,
    writeEmpty: Boolean(acc.write_empty_record),
  }
}

function buildPatch(draft: Draft, acc: Account, defaultCode: string | null): AccountUpdatePayload {
  const patch: AccountUpdatePayload = {}
  const name = draft.name.trim()
  if (name && name !== acc.name) patch.name = name
  if (draft.remark !== (acc.remark ?? '')) patch.remark = draft.remark || null
  Object.assign(patch, basicNotificationPatch(acc.execution_notification_code, draft.notificationCode, draft.feishu, draft.clearFeishu, defaultCode))

  if (draft.portfolioId !== acc.portfolio_id) patch.portfolio_id = draft.portfolioId

  const we = draft.writeEmpty ? 1 : 0
  if (we !== (acc.write_empty_record ?? 0)) patch.write_empty_record = we

  return patch
}

const FIELD_LABEL: Record<string, string> = {
  name: '名称',
  remark: '备注',
  feishu_key: '飞书',
  portfolio_id: '跟随组合',
  write_empty_record: '空仓记录',
}

function summarize(patch: Partial<Account>, acc: Account, portfolios: PortfolioLite[] | null): string[] {
  const pname = (v: number | null | undefined) =>
    v == null ? '未绑定' : (portfolios?.find((p) => p.id === v)?.name ?? `#${v}`)
  const out: string[] = []
  for (const k of Object.keys(patch)) {
    if (k === 'portfolio_id') out.push(`组合 ${pname(acc.portfolio_id)}→${pname(patch.portfolio_id)}`)
    else if (k === 'write_empty_record')
      out.push(`空仓记录 ${acc.write_empty_record ? '开' : '关'}→${patch.write_empty_record ? '开' : '关'}`)
    else if (k === 'execution_notification_code') continue
    else out.push(`${FIELD_LABEL[k] ?? k} 已改`)
  }
  return out
}

type EditSection = 'basic' | 'portfolio'

const SECTION_TITLE: Record<EditSection, string> = {
  basic: '基本信息',
  portfolio: '组合执行',
}

export function AccountEditPage({ section = 'basic' }: { section?: EditSection }) {
  const { id } = useParams()
  const accountId = Number(id)
  const toast = useToastStore((s) => s.toast)
  const accounts = useDomainStore((s) => s.accounts)
  const portfolios = useDomainStore((s) => s.portfolios)
  const portfoliosError = useDomainStore((s) => s.portfoliosError)
  const channelCatalogLoading = useChannelCatalogStore((s) => s.loading)
  const channelCatalogError = useChannelCatalogStore((s) => s.error)
  const refreshAccounts = useDomainStore((s) => s.refreshAccounts)

  const account = useOutletContext<AccountEditContext>()
  const acc = account.data
  const channelDescriptor = useChannelDescriptor(acc?.trade_channel)
  const item = accounts?.find((a) => a.account_id === accountId) ?? null
  // 首帧用仪表盘缓存名/渠道挂标题，避免等 getAccount 时落点缺失、FLIP 断档，
  // 也避免 Chip 在过渡尾巴上闪现。
  const displayName = acc?.name ?? item?.name

  const [defaultCode, setDefaultCode] = useState<string | null>(null)
  const [resetting, setResetting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [templateError, setTemplateError] = useState<Error | null>(null)
  const [draft, setDraft] = useState(() => draftOf(acc))
  const [feishuTest, setFeishuTest] = useState<AccountFeishuTestResult | 'busy' | null>(null)
  const [saveError, setSaveError] = useState<Error | null>(null)

  const resetDraft = () => {
    setDraft(draftOf(acc))
    setSaveError(null)
    setTemplateError(null)
    setFeishuTest(null)
  }

  /** 统一页头标题行（页名 · 账户名 + 渠道 chip）；账户名 FLIP 门控在原子内部。 */
  const titleName = (
    <AccountPageTitle
      accountId={accountId}
      page={(acc?.trade_channel ?? item?.trade_channel) === SIGNAL_CHANNEL && section === 'portfolio' ? '绑定组合' : SECTION_TITLE[section]}
      name={displayName}
      channel={acc?.trade_channel ?? item?.trade_channel}
      market={acc?.market ?? item?.market}
    />
  )

  const signalAccount = acc.trade_channel === SIGNAL_CHANNEL
  if (signalAccount && section === 'basic') return <Navigate to={`/accounts/${accountId}`} replace />

  const pageChrome = <div className="flex flex-wrap items-center gap-3">{titleName}</div>

  if (channelDescriptor == null && channelCatalogLoading) {
    return (
      <section className="pb-24">
        {pageChrome}
        <EditLoading bare />
      </section>
    )
  }

  const d = draft
  const set = (patch: Partial<Draft>) => {
    setSaveError(null)
    setDraft((prev) => ({ ...prev, ...patch }))
  }
  // 飞书 key 容忍粘贴整条 webhook 链接：账户专用测试接口与保存都使用规整后的裸 key，
  // 测试使用当前输入的机器人 key，无需先保存。
  const feishuKey = extractFeishuKey(d.feishu)
  const runFeishuTest = async () => {
    setFeishuTest('busy')
    try {
      setFeishuTest(await testAccountFeishu(accountId, feishuKey))
    } catch (e) {
      setFeishuTest({ ok: false, message: e instanceof Error ? e.message : String(e) })
    }
  }


  const notificationStatus = basicNotificationStatus(acc, d.notificationCode, d.clearFeishu, defaultCode)
  const resetNotification = async () => {
    if (resetting || saving) return
    setResetting(true)
    setTemplateError(null)
    try {
      const template = await getDefaultAccountNotification(accountId)
      setDefaultCode(template)
      set({ notificationCode: template })
    } catch (cause) {
      setTemplateError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      setResetting(false)
    }
  }
  const patch = buildPatch(d, acc, defaultCode)
  const changes = summarize(patch, acc, portfolios)
  const nextStatus = feishuKey && notificationStatus === 'none' && !d.clearFeishu ? 'default' : notificationStatus
  if ('execution_notification_code' in patch || nextStatus !== acc.execution_notification_status) {
    changes.push(`执行通知：${NOTIFICATION_STATUS_LABEL[acc.execution_notification_status]} → ${NOTIFICATION_STATUS_LABEL[nextStatus]}`)
  }
  const dirty = changes.length > 0
  const blocked = Boolean(
    portfolios == null || portfoliosError || channelCatalogError,
  )

  const save = async () => {
    if (saving || resetting) return
    if (!dirty) return toast('没有改动')
    setSaveError(null)
    setSaving(true)
    try {
      let template = defaultCode
      if (feishuKey && !d.notificationCode?.trim() && !d.clearFeishu && template === null) {
        template = await getDefaultAccountNotification(accountId)
        setDefaultCode(template)
      }
      const updated = await updateAccount(accountId, buildPatch(d, acc, template))
      setDraft(draftOf(updated))
      setFeishuTest(null)
      toast('账户已更新')
      void refreshAccounts()
      account.refresh()
    } catch (e) {
      setSaveError(e instanceof Error ? e : new Error(String(e)))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section>
      <fieldset disabled={saving}>
      {pageChrome}

      {section === 'basic' && (
        <>
        <Section label="基本信息">
          <Row label="名称">
            <input className={TEXT} value={d.name} onChange={(e) => set({ name: e.target.value })} />
          </Row>
          <Row label="备注">
            <input className={TEXT} value={d.remark} placeholder="可选" onChange={(e) => set({ remark: e.target.value })} />
          </Row>
        </Section>
        <Section label="执行通知">
          <Row label="通知方式" span top>
            <div className="flex flex-wrap items-center gap-3 pt-1 text-[14px]">
              <InkRewrite text={NOTIFICATION_STATUS_LABEL[notificationStatus]} tone="label" />
              {notificationStatus === 'function' && <>
              <Link className="inline-flex items-center gap-1 text-accent hover:underline" to={`/accounts/${accountId}/edit/notification`}>
                编辑与测试 <ExternalLink size={13} aria-hidden />
              </Link>
              <button type="button" disabled={saving || resetting} className="cursor-pointer text-accent disabled:opacity-45" onClick={() => void resetNotification()}>{resetting ? '读取中…' : '重置为默认'}</button>
              </>}
            </div>
          </Row>
          <ErrorNotice intent="mutation" title="默认模板读取失败" error={templateError} variant="compact" onRetry={() => void resetNotification()} />
          <div inert={notificationStatus === 'function'} className={`md:col-span-2 grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${notificationStatus === 'function' ? 'grid-rows-[0fr]' : 'grid-rows-[1fr]'}`}>
          <div className="min-h-0 overflow-hidden">
            <Row label="Webhook" hint={d.clearFeishu ? '保存后关闭' : (feishuKey || acc.feishu_configured ? '已配置' : '未配置')} top span>
            <div className="flex items-center gap-2">
              <FeishuWebhookInput value={d.feishu} configured={acc.feishu_configured} onChange={(feishu) => {
                set({ feishu, clearFeishu: false })
                if (feishuTest) setFeishuTest(null)
              }} />
              <button
                type="button"
                className="flex-none cursor-pointer text-[13px] text-ink-2 disabled:opacity-45"
                disabled={!acc.feishu_configured}
                onClick={() => {
                  set({ clearFeishu: !d.clearFeishu, feishu: '' })
                  setFeishuTest(null)
                }}
              >
                <InkRewrite text={d.clearFeishu ? '撤销清除' : '清除 Webhook'} tone="label" />
              </button>
              <button
                type="button"
                className="flex-none cursor-pointer rounded-[9px] border border-line bg-surface px-4 py-2 text-[15px] text-ink-2 transition-[border-color] hover:border-ink-3/40 disabled:opacity-45"
                disabled={(!feishuKey && !acc.feishu_configured) || d.clearFeishu || feishuTest === 'busy'}
                onClick={() => void runFeishuTest()}
              >
                {feishuTest === 'busy' ? '测试中…' : '测试推送'}
              </button>
            </div>
            {feishuTest && feishuTest !== 'busy' && (
              <div className={`mt-1.5 inline-flex items-center gap-1.5 text-[13px] ${feishuTest.ok ? 'text-accent' : 'text-warn'}`} role="status">
                {feishuTest.ok && <Check size={14} />}
                {feishuTest.message}
              </div>
            )}
            <Link
              className="mt-4 inline-flex items-center gap-1 text-[13px] text-ink-3 transition-colors hover:text-accent hover:underline"
              to={`/accounts/${accountId}/edit/notification`}
            >
              高级设置 · 自定义执行通知函数 <ExternalLink size={13} aria-hidden />
            </Link>
            </Row>
          </div>
          </div>
        </Section>
        </>
      )}

      {section === 'portfolio' && (
        <Section label={signalAccount ? '绑定组合' : '组合执行'}>
        {!signalAccount && <Row label="空仓写记录" hint="审计留痕">
          <Toggle ariaLabel="空仓写记录" on={d.writeEmpty} onClick={() => set({ writeEmpty: !d.writeEmpty })} />
        </Row>}
          <div className="mt-3 min-w-0 md:col-span-2">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="text-[14px] text-ink-2">跟随组合</h2>
              <button type="button" aria-pressed={d.portfolioId == null}
                disabled={portfolios == null || Boolean(portfoliosError)}
                onClick={() => set({ portfolioId: null })}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 text-[13px] text-ink-2 hover:bg-fill focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-45">
                {d.portfolioId == null && <Check size={14} aria-hidden />}不绑定组合
              </button>
            </div>
            <PortfolioChoiceList market={signalAccount ? acc.market : undefined} selectedId={d.portfolioId} onSelect={(portfolioId) => set({ portfolioId })} />
          </div>
        </Section>
      )}

      </fieldset>
      <EditSaveBar
        changes={changes}
        blocked={blocked || resetting}
        saving={saving}
        onCancel={() => { if (!saving && !resetting) resetDraft() }}
        onSave={() => void save()}
        error={saveError}
      />
    </section>
  )
}
