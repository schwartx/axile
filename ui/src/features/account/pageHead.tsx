/**
 * 账户域页头原子：返回按钮 + 大标题（``页名 · 账户名``）+ 渠道 chip。账户名也可返回概览，
 * 并按身份协议参与共享元素 FLIP。
 *
 * 身份协议（先命名同一逻辑物；FLIP 只在「身份对 + 几何真变」时才有信息量）：
 * - 挂名只服务几何真变的配对：详情头（hero 卡内 16/620）↔ 各页标题槽（内容顶 18/640），
 *   以及舰队卡 / 组合行 ↔ 详情头（这两对由卡片侧自行挂名，详情头常配对）。
 * - 标题槽页面之间互切（基本信息 ↔ 连接设置 ↔ … ↔ 组合执行）落点恒等：恒等 FLIP 没有
 *   信息量，反而把名字从工作区快照里抠到一条与 180ms 交叉淡**不同步的轨道**上，读起来
 *   像「晃」——故同槽互切不挂名，名字随工作区同轨淡换。
 * - 页名前缀是内容真变，不挂名，随工作区 180ms 交叉淡（同槽换字，诚实）。
 */
import { useViewTransitionState } from 'react-router'
import { Chip } from '@/components/ui/Card'
import { BackLink, type BackLinkProps } from '@/components/ui/BackLink'
import { Link } from '@/components/ui/nav'
import { channelLabel } from '@/features/dashboard/display'
import type { TradeChannel } from '@/types/api'

/**
 * 账户名共享元素门控：仅在「本次过渡涉及账户详情页」时为真。
 *
 * ViewTransitionContext 在整个过渡期间同时固定持有 currentLocation 与 nextLocation，
 * 新旧两侧用同一个精确路径判定即可一致挂名，无需逐路径登记。详情头是账户域内唯一
 * 与标题槽几何不同的名字落点；同槽标题页互切一律为假（恒等 FLIP 是假连续，摁死）。
 * 执行详情等不愿参与的页由 ``AccountPageTitle`` 的 ``flip={false}`` 关闭。
 */
// oxlint-disable-next-line react/only-export-components -- 门控 hook 与标题原子同属页头一处，刻意合并
export function useAccountNameVt(accountId: number): boolean {
  return useViewTransitionState(`/accounts/${accountId}`)
}

/**
 * 账户域统一大标题：``页名 · 账户名`` + 渠道 chip（返回片段，外层自行排行）。
 *
 * Parameters
 * ----------
 * page : string
 *     当前页名（与侧边栏一致）；不进共享身份，随工作区交叉淡。
 * name : string | null | undefined
 *     账户名；缺省时回退 ``账户 #id`` 且不挂共享名（身份未就位不做假连续）。
 * flip : boolean
 *     为假时账户名不做共享元素 FLIP（执行详情等主动退出的页）。
 * back : BackLinkProps | undefined
 *     默认返回当前账户概览；更深的详情页可指定所属列表及筛选参数。
 */
export function AccountPageTitle({
  accountId,
  page,
  name,
  channel,
  market,
  flip = true,
  back,
}: {
  accountId: number
  page: string
  name?: string | null
  channel?: TradeChannel
  market?: string
  flip?: boolean
  back?: BackLinkProps
}) {
  const transitioning = useAccountNameVt(accountId)
  const nameVt = flip && transitioning && name != null
  return (
    <>
      <div className="flex min-w-0 items-center gap-3">
        <BackLink {...(back ?? { to: `/accounts/${accountId}`, label: '返回账户概览' })} />
        <h1 className="min-w-0 text-[19px] font-[640] [overflow-wrap:anywhere]">
          {page} ·{' '}
          <Link
            to={`/accounts/${accountId}`}
            className="cursor-pointer hover:underline"
            style={nameVt ? { viewTransitionName: `account-name-${accountId}` } : undefined}
          >
            {name ?? `账户 #${accountId}`}
          </Link>
        </h1>
      </div>
      {channel && (
        // 渠道徽章与账户名同一逻辑单元：复用 nameVt 开关同轨飞行；name 不挂名的过渡
        // （同槽互切）徽章也绝不挂，避免独自被抠到不同步的轨道上「晃」。
        <Chip style={nameVt ? { viewTransitionName: `account-channel-${accountId}` } : undefined}>
          {channelLabel(channel, market ?? '')}
        </Chip>
      )}
    </>
  )
}
