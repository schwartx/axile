import { ChevronDown, FolderOpen, X } from 'lucide-react'
import { useId, useState } from 'react'

import { OverflowText } from '@/components/ui/OverflowText'
import { StringTagInput } from '@/components/ui/StringTagInput'

export function AlgorithmExtensions({
  directories,
  modules,
  onDirectoriesChange,
  onModulesChange,
  onBrowse,
}: {
  directories: string[]
  modules: string[]
  onDirectoriesChange: (directories: string[]) => void
  onModulesChange: (modules: string[]) => void
  onBrowse: () => void
}) {
  const id = useId()
  const [advancedOpen, setAdvancedOpen] = useState(false)

  return (
    <div className="min-w-0 py-3 md:col-span-2">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-[14px] leading-6 text-ink-2">
          选择存放算法的文件夹，保存并重启后即可在账户中选用。
        </p>
        <button
          type="button"
          className="inline-flex flex-none cursor-pointer items-center gap-2 rounded-[9px] border border-line bg-surface px-3 py-2 text-[14px] text-ink-1 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent"
          onClick={onBrowse}
        >
          <FolderOpen size={16} aria-hidden />
          添加算法目录
        </button>
      </div>
      <ul aria-label="已添加的算法目录">
        {directories.map((directory) => (
          <li key={directory} className="mt-3 flex min-w-0 items-center gap-3 rounded-[9px] border border-line px-3 py-2">
            <FolderOpen size={16} className="flex-none text-ink-3" aria-hidden />
            <OverflowText className="min-w-0 flex-1 font-mono text-[13px] text-ink-2" text={directory} />
            <button
              type="button"
              aria-label={`移除算法目录 ${directory}`}
              className="grid h-8 w-8 flex-none cursor-pointer place-items-center rounded-[7px] text-ink-2 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent"
              onClick={() => onDirectoriesChange(directories.filter((item) => item !== directory))}
            >
              <X size={15} aria-hidden />
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        aria-expanded={advancedOpen}
        aria-controls={`${id}-advanced`}
        className="mt-4 inline-flex cursor-pointer items-center gap-1.5 text-[13px] text-ink-2 hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent"
        onClick={() => setAdvancedOpen((open) => !open)}
      >
        <ChevronDown size={14} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${advancedOpen ? '' : '-rotate-90'}`} />
        按模块名添加（高级）
        {modules.length > 0 && <span className="text-ink-3">· 已配置 {modules.length} 项</span>}
      </button>
      <div
        id={`${id}-advanced`}
        inert={!advancedOpen}
        aria-hidden={!advancedOpen}
        className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${advancedOpen ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}
      >
        <div className="min-h-0 overflow-hidden">
          <label htmlFor={`${id}-modules`} className="mt-3 mb-2 block text-[13px] text-ink-2">Python 模块名</label>
          <StringTagInput
            id={`${id}-modules`}
            value={modules}
            mode="module"
            placeholder="package.module"
            help="仅用于已安装的算法包，通常无需填写。"
            onChange={onModulesChange}
          />
        </div>
      </div>
    </div>
  )
}
