import React, { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowUpRight, Loader2 } from 'lucide-react'
import apiClient from '../api/client'
import type { KnowledgeVersion } from '../types'
import Markdown from './Markdown'
import { formatKnowledgeTime } from './KnowledgeHistory'
import { scrollToKnowledgeAnchor, stripMatchingHeading } from '../lib/knowledgeNavigation'

export function KnowledgeVersionContent({ version, onOpenCurrent, onInternalLink }: {
  version: KnowledgeVersion; onOpenCurrent: (path: string, title: string) => void
  onInternalLink: (href: string, sourcePath: string) => void
}) {
  return <>
    <header className="mb-5 border-b border-default pb-4">
      <h2 className="text-lg font-semibold text-content">{version.title}</h2>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-content-secondary">
        <span>{version.current_status === 'current' ? '当前版本' : version.current_status === 'paused' ? '已暂停' : '历史版本'} · 只读</span>
        <span>{version.searchable ? '当前可检索' : '当前不可检索'}</span>
        <span>{version.category}</span><time dateTime={version.created_at}>{formatKnowledgeTime(version.created_at)}</time>
        {version.current_path && <button type="button" onClick={() => onOpenCurrent(version.current_path!, version.title)}
          className="inline-flex items-center gap-1 rounded-btn border border-default px-2 py-1 text-accent hover:bg-accent-soft">查看当前有效版<ArrowUpRight size={12} /></button>}
      </div>
    </header>
    <Markdown content={stripMatchingHeading(version.content, version.title)} onInternalLink={href => onInternalLink(href, version.path || '')} />
  </>
}

export default function KnowledgeVersionView({ publishId, agentId, onBack, onOpenCurrent, onInternalLink, anchor, onAnchorResolved }: {
  publishId: string; agentId?: string; onBack: () => void
  onOpenCurrent: (path: string, title: string) => void; onInternalLink: (href: string, sourcePath: string) => void
  anchor?: string; onAnchorResolved?: (found: boolean) => void
}) {
  const root = useRef<HTMLDivElement>(null)
  const [version, setVersion] = useState<KnowledgeVersion | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let active = true
    setVersion(null); setLoading(true); setError('')
    apiClient.getKnowledgeVersion(publishId, agentId).then(result => {
      if (!active) return
      if (result.status !== 'success') throw new Error(result.message || '读取历史版本失败')
      setVersion(result.version)
    }).catch(e => { if (active) setError(e.message || '读取历史版本失败') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [publishId, agentId, retry])
  useEffect(() => {
    if (loading || !version || !anchor) return
    const timer = setTimeout(() => onAnchorResolved?.(scrollToKnowledgeAnchor(root.current, anchor)), 0)
    return () => clearTimeout(timer)
  }, [loading, version, anchor, onAnchorResolved])
  return <div ref={root} className="mx-auto w-full max-w-3xl px-4 py-5 sm:px-6">
    <button type="button" onClick={onBack} className="mb-5 inline-flex items-center gap-1 text-sm text-content-secondary hover:text-accent"><ArrowLeft size={14} />返回整理记录</button>
    {loading ? <div role="status" className="flex items-center gap-2 text-sm text-content-tertiary"><Loader2 size={16} className="animate-spin" />核验历史版本…</div>
      : error ? <div role="alert" className="flex gap-3 text-sm text-danger">{error}<button onClick={() => setRetry(v => v + 1)} className="underline">重试</button></div>
        : version && <KnowledgeVersionContent version={version} onOpenCurrent={onOpenCurrent} onInternalLink={onInternalLink} />}
  </div>
}
