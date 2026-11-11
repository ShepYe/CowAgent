import React, { useEffect, useRef, useState } from 'react'
import { Check, Loader2, Lock, RotateCcw, Tags, X } from 'lucide-react'
import type { KnowledgeCatalogPage } from '../types'

export default function KnowledgeCategoryMenu({ page, categories, onChange }: {
  page: KnowledgeCatalogPage; categories: string[]
  onChange: (page: KnowledgeCatalogPage, category?: string) => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  return <>
    <button ref={trigger} type="button" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}
      className="inline-flex shrink-0 items-center gap-1.5 rounded-btn border border-default px-2.5 py-1.5 text-xs text-content-secondary hover:bg-surface-2">
      {page.category_mode === 'manual' ? <Lock size={13} /> : <Tags size={13} />}
      分类 · {page.category_mode === 'manual' ? '手动' : '自动'}
    </button>
    {open && <CategoryDialog key={page.page_id} page={page} categories={categories} onChange={onChange}
      onClose={() => { setOpen(false); trigger.current?.focus() }} />}
  </>
}

function CategoryDialog({ page, categories, onChange, onClose }: {
  page: KnowledgeCatalogPage; categories: string[]; onChange: (page: KnowledgeCatalogPage, category?: string) => Promise<void>; onClose: () => void
}) {
  const dialog = useRef<HTMLDivElement>(null)
  const [category, setCategory] = useState(page.category)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    dialog.current?.querySelector<HTMLInputElement>('input')?.focus()
  }, [])
  const submit = async (reset = false) => {
    if (busy || (!reset && !category.trim())) return
    setBusy(true); setError('')
    try { await onChange(page, reset ? undefined : category.trim()); onClose() }
    catch (e) { setError(e instanceof Error ? e.message : '分类保存失败'); setBusy(false) }
  }
  const options = [...new Set([page.category, ...categories])].filter(Boolean).sort((a, b) => a.localeCompare(b))
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onMouseDown={e => { if (e.target === e.currentTarget && !busy) onClose() }}>
    <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby="knowledge-category-title"
      className="w-full max-w-md rounded-xl border border-default bg-surface p-5 shadow-xl" onKeyDown={e => {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); if (!busy) onClose() }
        if (e.key === 'Tab') {
          const items = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [tabindex="0"]') || [])
          const first = items[0], last = items[items.length - 1]
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus() }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
        }
      }}>
      <div className="mb-4 flex items-center gap-3">
        <h2 id="knowledge-category-title" className="min-w-0 flex-1 text-base font-semibold text-content">知识页分类</h2>
        <span className="text-content-tertiary" title="手动分类会保持到恢复自动。恢复自动保留当前分类，后续整理可调整；不改变知识正文或出处。">?</span>
        <button type="button" aria-label="关闭分类" disabled={busy} onClick={onClose} className="rounded-btn p-1.5 text-content-secondary hover:bg-surface-2"><X size={16} /></button>
      </div>
      <form onSubmit={e => { e.preventDefault(); void submit() }}>
        <label className="block text-sm text-content-secondary">选择或新建分类
          <input value={category} disabled={busy} onChange={e => setCategory(e.target.value)}
            className="mb-3 mt-2 w-full rounded-btn border border-strong bg-inset px-3 py-2 text-sm text-content focus:border-accent focus:outline-none" />
        </label>
        <div role="group" aria-label="已有分类" className="mb-4 max-h-52 overflow-y-auto rounded-lg border border-default p-1">
          {options.map(option => <button key={option} type="button" disabled={busy} aria-pressed={category === option} onClick={() => setCategory(option)}
            className={`flex w-full items-center gap-2 rounded-btn px-3 py-2 text-left text-sm ${category === option ? 'bg-accent-soft text-accent' : 'text-content-secondary hover:bg-surface-2'}`}>
            <span className="flex-1 truncate">{option}</span>{category === option && <Check size={14} />}
          </button>)}
        </div>
        {error && <p role="alert" className="mb-3 text-sm text-danger">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2">
          {page.category_mode === 'manual' && <button type="button" disabled={busy} onClick={() => void submit(true)}
            className="mr-auto inline-flex items-center gap-1 rounded-btn border border-default px-3 py-2 text-sm text-content-secondary disabled:opacity-50"><RotateCcw size={13} />恢复自动</button>}
          <button type="button" disabled={busy} onClick={onClose} className="rounded-btn border border-default px-3 py-2 text-sm text-content-secondary">取消</button>
          <button type="submit" disabled={busy || !category.trim()} className="inline-flex items-center gap-1 rounded-btn bg-accent px-3 py-2 text-sm text-accent-contrast hover:bg-accent-hover disabled:opacity-50">
            {busy && <Loader2 size={14} className="animate-spin" />}保存分类
          </button>
        </div>
      </form>
    </div>
  </div>
}
