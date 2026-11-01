---
name: Axile 管理界面
description: 沿用交易驾驶舱体系的操作界面
colors:
  primary: "var(--color-accent)"
  primary-soft: "var(--color-accent-soft)"
  warning: "var(--color-warn)"
  market-up: "var(--color-up)"
  market-down: "var(--color-down)"
  neutral-bg: "var(--color-bg)"
  neutral-surface: "var(--color-surface)"
  neutral-ink: "var(--color-ink-1)"
  neutral-secondary: "var(--color-ink-2)"
  neutral-muted: "var(--color-ink-3)"
  neutral-line: "var(--color-line)"
  neutral-border: "var(--color-border-strong)"
  neutral-fill: "var(--color-fill)"
typography:
  body:
    fontFamily: "var(--font-sans)"
    lineHeight: 1.5
  title:
    fontFamily: "var(--font-sans)"
    fontSize: "19px"
    fontWeight: 640
  label:
    fontFamily: "var(--font-sans)"
    fontSize: "12px"
  mono:
    fontFamily: "var(--font-mono)"
rounded:
  card: "var(--radius-card)"
  chip: "var(--radius-chip)"
components:
  button-secondary:
    textColor: "{colors.neutral-secondary}"
    rounded: "{rounded.chip}"
    padding: "0 12px"
  replay-play:
    backgroundColor: "{colors.primary-soft}"
    textColor: "{colors.primary}"
    rounded: "{rounded.chip}"
    padding: "0 12px"
  card:
    backgroundColor: "{colors.neutral-surface}"
    rounded: "{rounded.card}"
  chip:
    backgroundColor: "{colors.neutral-fill}"
    textColor: "{colors.neutral-secondary}"
    rounded: "{rounded.chip}"
    padding: "3px 10px"
---

# Design System: Axile 管理界面

## Overview

**Creative North Star: "交易驾驶舱"**

这是现有管理界面的代码扫描记录，继承 [axon-frontend-language](../../.claude/skills/axon-frontend-language/SKILL.md)。结构保持紧凑、安静，颜色与动效服务于状态判读和信息连续性。

[theme.css](src/styles/theme.css) 是颜色、字体、圆角与 keyframes 的代码真源。上面的变量引用随明暗主题、增强对比度与行情偏好解析；本文和侧车不提供另一份可独立编辑的配色表。修改视觉规则时先修改真源，再更新记录。

**Key Characteristics:**

- 中性面板、细分隔线和小圆角组织信息。
- 行情方向、操作与质量各自使用语义通道。
- 复用既有控件与过渡入口，保留键盘和减少动态效果路径。

## Colors

### Primary

信号色使用 `primary`；选中背景使用 `primary-soft`。不把普通健康状态持续刷成强调色。

### Neutral

`neutral-bg` 是工作区底色，`neutral-surface` 是面板；三级文字、分隔线、较强边框与填充分别承担阅读层级和控件边界。

**The Semantic Channels Rule.** 行情方向使用 `market-up/down`，操作与有利质量使用 `primary`，不利质量和警告使用 `warning`；正常、持平和未知保持中性。状态文字、符号、位置或线型补充颜色。

## Typography

正文沿用 Space Grotesk Variable 与系统中文回退，标识和代码可用 JetBrains Mono Variable；完整字体栈由 CSS 管理。数字使用等宽数字特性。

回放页标题沿用其他管理页的 20px 半粗字（`title`），详情标题为 15px，明细正文和主要读数为 13–14px，图例、字段标签和次要时间为 12px。这些是当前表面密度，不是全站正文字号下限；没有新增展示字体。

## Layout

应用根节点锁定页面滚动，工作区按路由管理内部滚动。全局导航壳保持稳定，内容区允许收缩；表格和长说明在自己的区域滚动。

回放的具体构图、断点和验收条件见 [回放方向说明](.impeccable/replay-direction.md) 与 [replay.css](src/features/replay/replay.css)，不推广为所有页面的固定布局。当前回放沿用 20px 内容留白和既有页标题，但保留 Bookmap 式大幅主图。图表横向占满工作区，底部 144px 为订单列表与 300px 宽的简要详情，完整字段在原生 popover 中按需查看。播放、步进、倍速与时间轴独立固定在底部，读取窗口与录制段选择放在录制信息中。市场价格线只连接真实记录的市场成交价，未来、冲突和缺口不补线；选中委托标注历史活动段的当时剩余量。用户明确以桌面验收，不扩展手机适配。

## Elevation & Depth

默认面板靠底色和细边框区分层次，`Card` 本身不带阴影。需要的轻阴影沿用 `--shadow-card`；菜单、滑块和原生弹层按既有组件实现，不增加装饰性悬浮层。

## Shapes

面板与胶囊复用 `card/chip` 圆角。分段、输入和下拉另有既存的小圆角实例；本文不把这些局部尺寸改造成新的全站刻度。

## Components

- **按钮与选择器**：回放次级按钮保持中性细边框，播放按钮用浅强调底；保留 disabled、可访问名称与清晰焦点。回放品种、倍速、录制段和窗口设置统一复用 `Select`；单一品种直接显示名称。
- **面板与标签**：[Card / Chip](src/components/ui/Card.tsx) 提供面板和中性标签；内部留白由调用处决定。标签不是独立的成功指示灯。
- **输入**：[ConnectionFieldInput](src/components/ui/ConnectionFieldInput.tsx) 保留标签、警告文案和焦点边框；侧车仅展示其视觉结构，不复制连接、剪贴板或密文行为。
- **导航与切换**：导航使用 [nav.tsx](src/components/ui/nav.tsx)，页内过渡使用 [viewTransition.ts](src/lib/viewTransition.ts)，分段使用 [Segmented](src/components/ui/Segmented.tsx)。分段选中态保留滑块、文字权重与 `aria-pressed`。
- **文字连续性**：同槽换字使用 [InkRewrite](src/components/ui/InkRewrite.tsx)，单行截断补偿使用 [OverflowText](src/components/ui/OverflowText.tsx)。关键内容保留静态可达路径；不另建跑马灯或淡入系统。

## Do's and Don'ts

### Do:

- **Do** 从 CSS 真源和既有组件消费语义、主题与交互规则。
- **Do** 保留键盘、焦点、减少动态效果及非颜色线索。
- **Do** 将单页构图和验收证据留在对应表面说明中。

### Don't:

- **Don't** 把涨跌色当作成功或失败色，或用强调色反复确认健康。
- **Don't** 为首次出现添加表演，或为同一对象叠加两套连续性机制。
- **Don't** 把当前截图、偏好开关或本文当作全站 WCAG、性能或交易验收证明。

回放页头直接使用账户域的 `AccountPageTitle`，显示“执行回放 · 账户名”及渠道标签；播放定位复用自绘 `ValueSlider`，拖动只预览，结束操作后提交，读数与刻度统一格式化为时间。
