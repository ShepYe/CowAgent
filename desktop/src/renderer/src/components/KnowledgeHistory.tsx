import React, { useEffect, useState } from 'react'
import { ArrowLeft, ArrowRight, Clock3, Loader2 } from 'lucide-react'
import apiClient from '../api/client'
import type { KnowledgeHistory as HistoryData, KnowledgeHistoryEvent } from '../types'
import Markdown from './Markdown'

export const knowledgeEventLabels: Record<KnowledgeHistoryEvent['event_type'], string> = {
  published: '发布知识', updated: '更新知识', category_changed: '手动分类', category_auto: '恢复自动分类',
}

export function KnowledgeEventRows({ events, onOpenVersion }: { events: KnowledgeHistoryEvent[]; onOpenVersion: (publishId: string) => void }) {
  return <ol className="divide-y divide-default">
    {events.map(event => <li key={event.id} className="flex gap-3 py-4">
      <Clock3 size={15} className="mt-1 shrink-0 text-content-tertiary" />
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-content-tertiary">
          <span>{knowledgeEventLabels[event.event_type]}</span>
          <time dateTime={event.created_at}>{formatKnowledgeTime(event.created_at)}</time>
        </div>
        <button type="button" onClick={() => onOpenVersion(event.publish_id)} className="text-left text-sm font-medium text-content hover:text-accent">{event.title}</button>
        <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-content-secondary">
          <span>{event.category}</span>
          <span>{event.current_status === 'current' ? '当前版本' : event.current_status === 'paused' ? '已暂停' : '历史版本'}</span>
          <span>{event.searchable ? '当前可检索' : '当前不可检索'}</span>
        </div>
      </div>
    </li>)}
  </ol>
}

export function formatKnowledgeTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未登记' : date.toLocaleString('zh-CN', { hour12: false })
}

export default function KnowledgeHistory({ agentId, revision, onOpenVersion, onInternalLink }: {
  agentId?: string; revision: number; onOpenVersion: (publishId: string) => void; onInternalLink: (href: string) => void
}) {
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<HistoryData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const limit = 20
  useEffect(() => {
    let active = true
    setLoading(true); setError('')
    apiClient.getKnowledgeHistory(offset, limit, agentId).then(result => {
      if (!active) return
      if (result.status !== 'success') throw new Error(result.message || '读取整理记录失败')
      setData(result)
    }).catch(e => { if (active) setError(e.message || '读取整理记录失败') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [agentId, offset, revision, retry])
  return <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-6">
    <h2 className="mb-5 text-lg font-semibold text-content">整理记录</h2>
    {error ? <div role="alert" className="flex items-center gap-3 text-sm text-danger">{error}<button onClick={() => setRetry(v => v + 1)} className="underline">重试</button></div>
      : loading ? <div role="status" className="flex items-center gap-2 py-5 text-sm text-content-tertiary"><Loader2 size={16} className="animate-spin" />读取中…</div>
        : data && <>
          <KnowledgeEventRows events={data.events} onOpenVersion={onOpenVersion} />
          {!data.events.length && <p className="py-5 text-sm text-content-tertiary">暂无已登记事件</p>}
          <div className="my-4 flex flex-wrap items-center justify-between gap-3 border-t border-default pt-4 text-sm">
            <button type="button" disabled={offset === 0} onClick={() => setOffset(v => Math.max(0, v - limit))} className="flex items-center gap-1 rounded-btn border border-default px-3 py-1.5 disabled:opacity-40"><ArrowLeft size={14} />上一页</button>
            <span className="text-xs text-content-tertiary">{data.total ? `${offset + 1}–${offset + data.events.length} / ${data.total}` : '0 条记录'}</span>
            <button type="button" disabled={!data.has_more} onClick={() => setOffset(v => v + limit)} className="flex items-center gap-1 rounded-btn border border-default px-3 py-1.5 disabled:opacity-40">下一页<ArrowRight size={14} /></button>
          </div>
          {data.legacy_content && <details className="mt-5 border-t border-default pt-4 text-sm text-content-secondary">
            <summary className="cursor-pointer">原有整理记录 <span title="保留原有文字记录；未登记的时间与事件不会补造。">?</span></summary>
            <div className="mt-4"><Markdown content={data.legacy_content} onInternalLink={onInternalLink} /></div>
          </details>}
        </>}
  </div>
}
