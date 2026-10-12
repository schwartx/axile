import { defineConfig } from 'vitepress'

export default defineConfig({
  lang: 'zh-CN',
  title: 'Axile',
  description: 'Axile 使用文档：连接账户、编写组合函数、完成调仓并核对执行结果。',
  base: '/axile/',
  srcDir: 'site',
  cleanUrls: false,
  lastUpdated: true,
  head: [
    ['link', { rel: 'icon', href: '/axile/favicon.svg', type: 'image/svg+xml' }],
    ['meta', { name: 'theme-color', content: '#078b9f' }],
  ],
  sitemap: { hostname: 'https://zengbin93.github.io/axile/' },
  themeConfig: {
    logo: '/favicon.svg',
    siteTitle: 'Axile 使用文档',
    nav: [
      { text: '使用指南', link: '/guide/introduction' },
      { text: '函数参考', link: '/reference/context' },
      { text: '0.19.1', link: 'https://github.com/zengbin93/axile/releases' },
    ],
    sidebar: [
      { text: '开始使用', items: [
        { text: '01 · 认识 Axile', link: '/guide/introduction' },
        { text: '02 · 安装与启动', link: '/guide/install' },
        { text: '03 · 第一个组合', link: '/guide/first-portfolio' },
      ] },
      { text: '理解与运行', items: [
        { text: '04 · 目标与持仓', link: '/guide/targets' },
        { text: '05 · 自定义组合函数', link: '/guide/functions' },
        { text: '06 · 日常运行', link: '/guide/daily' },
        { text: '07 · 结果与排错', link: '/guide/results' },
      ] },
      { text: '08 · 进阶参考', items: [
        { text: '参考入口', link: '/reference/' },
        { text: 'Context 查询接口', link: '/reference/context' },
        { text: '通知函数', link: '/reference/notifications' },
        { text: '执行算法', link: '/reference/algorithms' },
        { text: '渠道与配置', link: '/reference/channels' },
      ] },
    ],
    socialLinks: [{ icon: 'github', link: 'https://github.com/zengbin93/axile' }],
    search: {
      provider: 'local',
      options: {
        miniSearch: {
          options: {
            tokenize: (text) => Array.from(
              new Intl.Segmenter('zh-CN', { granularity: 'word' }).segment(text),
            ).filter(segment => segment.isWordLike).map(segment => segment.segment),
          },
        },
        locales: { root: { translations: {
          button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
          modal: { displayDetails: '显示详细结果', resetButtonTitle: '清空搜索',
            backButtonTitle: '关闭搜索', noResultsText: '没有找到相关内容',
            footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' } },
        } } },
      },
    },
    outline: { label: '本页内容', level: [2, 3] },
    docFooter: { prev: '上一页', next: '下一页' },
    lastUpdated: { text: '最后更新', formatOptions: { dateStyle: 'medium' } },
    editLink: {
      pattern: 'https://github.com/zengbin93/axile/edit/main/docs/site/:path',
      text: '在 GitHub 上改进此页',
    },
    returnToTopLabel: '回到顶部',
    sidebarMenuLabel: '目录',
    darkModeSwitchLabel: '主题',
    lightModeSwitchTitle: '切换浅色主题',
    darkModeSwitchTitle: '切换深色主题',
    footer: {
      message: 'Axile 公共核心使用文档',
      copyright: 'Apache-2.0 · Axile',
    },
  },
})
