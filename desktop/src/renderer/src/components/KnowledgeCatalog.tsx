import React, { useState } from 'react'
import { ChevronDown, ChevronRight, FileText, Lock, Search } from 'lucide-react'
import type { KnowledgeCatalogPage } from '../types'
import { groupCatalog } from '../lib/knowledgeNavigation'
import Markdown from './Markdown'

export function KnowledgeCatalogGroups({ pages, search = '', activePath, onOpen, compact = false }: {
  pages: KnowledgeCatalogPage[]; search?: string; activePath?: string | null
  onOpen: (path: string, title: string) => void; compact?: boolean
}) {
  const groups = groupCatalog(pages, search)
  return <div className="space-y-4">
    {groups.map(([category, rows]) => <CatalogGroup key={category} category={category} rows={rows}
      activePath={activePath} onOpen={onOpen} compact={compact} />)}
    {!groups.length && <p className="px-2 py-5 text-sm text-content-tertiary">{search ? '没有匹配的知识页' : '暂无当前有效知识页'}</p>}
  </div>
}

function CatalogGroup({ category, rows, activePath, onOpen, compact }: {
  category: string; rows: KnowledgeCatalogPage[]; activePath?: string | null
  onOpen: (path: string, title: string) => void; compact: boolean
}) {
  const [expanded, setExpanded] = useState(true)
  return <section>
    <button type="button" aria-expanded={expanded} onClick={() => setExpanded(v => !v)}
      className="flex w-full items-center gap-2 rounded-btn px-2 py-2 text-left text-sm font-medium text-content-secondary hover:bg-surface-2">
      {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      <span className="min-w-0 flex-1 truncate">{category.replace(/\//g, ' / ')}</span>
      <span className="text-xs text-content-tertiary">{rows.length}</span>
    </button>
    {expanded && <div className={compact ? 'ml-2 border-l border-default pl-1' : 'divide-y divide-default rounded-lg border border-default'}>
      {rows.map(page => <button key={page.page_id} type="button" onClick={() => onOpen(page.path, page.title)}
        className={`flex w-full items-center gap-2 rounded-btn px-3 ${compact ? 'py-2' : 'py-3'} text-left text-sm ${activePath === page.path ? 'bg-accent-soft text-accent' : 'text-content hover:bg-surface-2'}`}>
        <FileText size={14} className="shrink-0 text-content-tertiary" />
        <span className="min-w-0 flex-1 truncate">{page.title}</span>
        {page.category_mode === 'manual' && <span title="手动分类，后续整理保持此分类" aria-label="手动分类"><Lock size={12} /></span>}
        {!compact && <ChevronRight size={14} className="text-content-tertiary" />}
      </button>)}
    </div>}
  </section>
}

export default function KnowledgeCatalog({ pages, onOpen, legacyContent, onInternalLink, imageBaseDir }: {
  pages: KnowledgeCatalogPage[]; onOpen: (path: string, title: string) => void
  legacyContent?: string; onInternalLink?: (href: string) => void; imageBaseDir?: string
}) {
  const [search, setSearch] = useState('')
  return <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-6">
    <div className="mb-5 flex flex-wrap items-center gap-3">
      <h2 className="flex-1 text-lg font-semibold text-content">知识索引</h2>
      <span className="text-xs text-content-tertiary">{pages.filter(p => p.searchable).length} 个当前知识页</span>
    </div>
    <div className="relative mb-5">
      <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-content-tertiary" />
      <input aria-label="搜索当前知识目录" value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索标题或分类"
        className="w-full rounded-btn border border-strong bg-inset py-2 pl-9 pr-3 text-sm text-content focus:border-accent focus:outline-none" />
    </div>
    <KnowledgeCatalogGroups pages={pages} search={search} onOpen={onOpen} />
    {legacyContent?.trim() && <details className="mt-6 border-t border-default pt-4 text-sm text-content-secondary">
      <summary className="cursor-pointer">原索引与说明</summary><div className="mt-4">
        <Markdown content={legacyContent} onInternalLink={onInternalLink} imageBaseDir={imageBaseDir} />
      </div>
    </details>}
  </div>
}
