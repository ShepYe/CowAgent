import Markdown from '../components/Markdown'
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowUpRight, BookOpen, ChevronDown, FileText, History, Info, Loader2, Plus, RefreshCw, Search, Settings2, Upload, X } from 'lucide-react'
import { askConfirm } from '../store/confirmStore'
import Tooltip from '../components/Tooltip'
import { Btn, Modal, TextInput } from './settings/primitives'
import apiClient, { type PersonalTask, type PersonalSourceAction, type PersonalReference } from '../api/client'
import { buildMaterials, materialSource, materialResults, materialKnowledgeLabel, materialReorganizeSource, readableFilename, taskActions,
  taskStatuses as statuses, taskStages as stages, personalTaskTitle, personalOutcomeLabel, personalKnowledgeFingerprint,
  canResubmit, isTransportFilename, type Material, type SourceRef } from './personalMaterials'
export { personalTaskTitle, personalOutcomeLabel, personalKnowledgeFingerprint } from './personalMaterials'

type RunAction = (task: PersonalTask, name: string, source?: PersonalSourceAction, input?: string) => Promise<boolean>
interface PanelProps {
  onOpenKnowledge?: (path: string, title: string) => void
  onKnowledgeChanged?: (fingerprint: string) => boolean | Promise<boolean>
}
export default function PersonalTasksPanel({ onOpenKnowledge, onKnowledgeChanged }: PanelProps) {
  const [tasks, setTasks] = useState<PersonalTask[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [reference, setReference] = useState<{ taskId: string; publishId: string } | null>(null)
  const [referenceVersion, setReferenceVersion] = useState(0)
  const [message, setMessage] = useState('')
  const [success, setSuccess] = useState('')
  const [busy, setBusy] = useState('')
  const [uploadOpen, setUploadOpen] = useState(false)
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const requestId = useRef(crypto.randomUUID())
  const resubmitRequestIds = useRef(new Map<string, string>())
  const fileInput = useRef<HTMLInputElement>(null)
  const fingerprint = useRef<string | null>(null)
  const notifying = useRef(false)
  const changedCallback = useRef(onKnowledgeChanged)
  useEffect(() => { changedCallback.current = onKnowledgeChanged }, [onKnowledgeChanged])
  const load = useCallback(async () => {
    try {
      const res = await apiClient.personalTasks()
      if (res.status !== 'success') throw new Error(res.message || '读取任务失败')
      setTasks(res.tasks)
      setMessage('')
      setLoading(false)
      const next = personalKnowledgeFingerprint(res.tasks)
      if (fingerprint.current !== next && !notifying.current) {
        notifying.current = true
        try {
          if (await changedCallback.current?.(next) !== false) fingerprint.current = next
        } finally { notifying.current = false }
      }
    } catch (e) { setMessage(e instanceof Error ? e.message : '读取任务失败') }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void load(); const timer = setInterval(() => void load(), 4000); return () => clearInterval(timer) }, [load])
  const materials = useMemo(() => buildMaterials(tasks), [tasks])
  const visible = materials.filter(item => item.title.toLowerCase().includes(search.toLowerCase()))
  const active = materials.find(item => item.key === selected)
  const uploadTrigger = useRef<HTMLButtonElement>(null)
  const closeUpload = useCallback(() => { setUploadOpen(false); uploadTrigger.current?.focus() }, [])
  useEffect(() => {
    if (!uploadOpen) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') closeUpload() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [uploadOpen, closeUpload])
  const submit = async () => {
    if (!file || busy) return
    if (file.size > 100 * 1024 * 1024) { setMessage('文件超过 100 MiB 上限'); return }
    setBusy('submit'); setMessage(''); setSuccess('')
    try {
      const res = await apiClient.submitPersonal(file, text, requestId.current)
      if (res.status !== 'success') throw new Error(res.message || '提交失败')
      setFile(null); setText(''); requestId.current = crypto.randomUUID()
      if (fileInput.current) fileInput.current.value = ''
      closeUpload(); await load()
    } catch (e) { setMessage(e instanceof Error ? e.message : '提交失败') }
    finally { setBusy('') }
  }
  const action: RunAction = async (task, name, source, input) => {
    if (busy || (name === 'resubmit' && !canResubmit(task))) return false
    if (name === 'withdraw' && !(await askConfirm({ titleKey: 'personal_withdraw', msgKey: 'personal_withdraw_confirm', okKey: 'personal_withdraw' }))) return false
    setBusy(task.task_id); setMessage(''); setSuccess('')
    try {
      let resubmitId: string | undefined
      if (name === 'resubmit') {
        const key = `${task.task_id}:${task.attempt}`
        resubmitId = resubmitRequestIds.current.get(key) ?? crypto.randomUUID()
        resubmitRequestIds.current.set(key, resubmitId)
      }
      const res = await apiClient.personalAction(task, name, input, source, resubmitId)
      if (res.status !== 'success') throw new Error(res.message || '操作失败')
      setSuccess(name === 'reorganize' || name === 'resubmit' ? '已创建处理任务' : '操作成功')
      setReferenceVersion(v => v + 1)
      return true
    } catch (e) { setMessage(e instanceof Error ? e.message : '操作失败'); return false }
    finally { await load(); setBusy('') }
  }
  return <div className="flex-1 min-h-0 min-w-0 flex flex-col border-t border-default bg-base text-sm">
    <div className="flex items-center gap-3 px-4 py-3 border-b border-default bg-surface">
      <div className="flex-1 min-w-0 relative"><Search size={15} className="absolute left-3 top-2.5 text-content-tertiary" /><TextInput aria-label="搜索资料" placeholder="搜索资料" value={search} onChange={e => setSearch(e.target.value)} className="pl-9" /></div>
      <Tooltip label="刷新资料"><button aria-label="刷新资料" onClick={() => void load()} className="p-2 rounded-btn text-content-secondary hover:bg-surface-2"><RefreshCw size={16} /></button></Tooltip>
      <button ref={uploadTrigger} onClick={() => setUploadOpen(true)} className="inline-flex items-center gap-1.5 px-3 py-2 rounded-btn bg-accent text-accent-contrast hover:bg-accent-hover"><Plus size={15} />添加资料</button>
    </div>
    {message && <div role="alert" className="px-4 py-2 text-danger bg-danger-soft">{message}</div>}
    {success && <div role="status" className="px-4 py-2 text-accent bg-accent-soft">{success}</div>}
    {loading ? <div role="status" className="flex flex-1 items-center justify-center gap-2 text-content-tertiary"><Loader2 size={18} className="animate-spin" />读取资料…</div> : <MaterialsLayout materials={visible} active={active} selected={selected} onSelect={setSelected} onBack={() => setSelected(null)}>
      {active && <MaterialDetail key={active.key} material={active} busy={!!busy} onAction={action} onOpenKnowledge={onOpenKnowledge}
        onReference={(task, ref) => setReference({ taskId: task.task_id, publishId: String(ref.publish_id) })} />}
    </MaterialsLayout>}
    <Modal open={uploadOpen} title="添加资料" onClose={closeUpload} footer={<><Btn onClick={closeUpload}>取消</Btn><Btn variant="primary" disabled={!file || !!busy} onClick={() => void submit()}>{busy === 'submit' ? '接收中…' : '发送资料'}</Btn></>}>
      <input ref={fileInput} className="hidden" tabIndex={-1} aria-label="选择资料文件" type="file" accept=".pdf,.docx,.pptx,.xlsx,.md,.txt" disabled={!!busy}
        onChange={e => { setFile(e.target.files?.[0] || null); requestId.current = crypto.randomUUID() }} />
      <button onClick={() => fileInput.current?.click()} disabled={!!busy} className="w-full rounded-card border border-dashed border-strong bg-inset px-5 py-7 text-content-secondary hover:border-accent transition-colors flex flex-col items-center gap-2">
        <Upload size={22} className="text-accent" /><span>{file ? readableFilename(file.name) : '选择资料文件'}</span>
      </button>
      <TextInput autoFocus aria-label="资料用途" placeholder="补充用途（可选）" value={text} disabled={!!busy} onChange={e => { setText(e.target.value); requestId.current = crypto.randomUUID() }} />
      <Tooltip label="支持 PDF、DOCX、PPTX、XLSX、MD、TXT，单件不超过 100 MiB。助手自主整理；仅临时使用时请在用途中说明。"><button aria-label="资料格式与用途说明" className="text-content-tertiary"><Info size={14} /></button></Tooltip>
    </Modal>
    <Modal open={!!reference} size="lg" title="资料出处" onClose={() => setReference(null)}>
      {reference && <ReferenceBrowser taskId={reference.taskId} publishId={reference.publishId}
        version={`${referenceVersion}:${JSON.stringify(tasks.find(t => t.task_id === reference.taskId)?.result_refs.map(r => [r.current_searchable, r.source_state, (r.sources as SourceRef[] | undefined)?.map(s => s.source_state)]))}`}
        onClose={() => setReference(null)} />}
    </Modal>
  </div>
}

export function MaterialsLayout({ materials, active, selected, onSelect, onBack, children }: {
  materials: Material[]; active?: Material; selected: string | null; onSelect: (key: string) => void; onBack: () => void; children?: React.ReactNode
}) {
  const listRef = useRef<HTMLDivElement>(null)
  const detailRef = useRef<HTMLDivElement>(null)
  useEffect(() => { if (selected && active) detailRef.current?.focus() }, [selected, !!active])
  const back = () => { const selectedRow = listRef.current?.querySelector<HTMLButtonElement>('[aria-current="true"]'); onBack(); requestAnimationFrame(() => selectedRow?.focus()) }
  const documents = materials.filter(item => !item.recordOnly)
  const records = materials.filter(item => item.recordOnly)
  const row = (material: Material) => <button key={material.key} aria-current={selected === material.key} onClick={() => onSelect(material.key)}
    className={`w-full text-left rounded-btn px-3 py-3 mb-1 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent ${selected === material.key ? 'bg-accent-soft text-accent' : 'text-content hover:bg-surface-2'}`}>
    <div className="flex items-start gap-2"><FileText size={16} className="shrink-0 mt-0.5 opacity-70" /><span className="font-medium break-words line-clamp-2">{material.title}</span></div>
    <div className="pl-6 mt-1.5 text-xs text-content-tertiary">{material.recordOnly ? '独立处理记录' : materialKnowledgeLabel(material)}</div>
    <div className="pl-6 mt-1 text-xs text-content-secondary">最近处理：{statuses[material.tasks[0].status] || material.tasks[0].status}</div>
  </button>
  return <div className="flex-1 min-h-0 min-w-0 flex">
    <div ref={listRef} aria-label="资料列表" className={`${active ? 'hidden lg:block' : 'block'} w-full lg:w-72 xl:w-80 shrink-0 overflow-y-auto border-r border-default bg-surface p-2`}>
      <div className="px-3 py-2 text-xs text-content-tertiary">资料 · {documents.length}</div>
      {!materials.length && <p className="px-3 py-8 text-content-tertiary text-center">暂无资料</p>}
      {documents.map(row)}
      {records.length > 0 && <details open={active?.recordOnly || undefined} className="mt-3 border-t border-default pt-3" aria-label="其他处理记录">
        <summary className="cursor-pointer px-3 py-2 text-xs text-content-secondary">其他处理记录（{records.length}）</summary>
        {records.map(row)}
      </details>}
    </div>
    <div ref={detailRef} tabIndex={-1} aria-label="资料详情" onKeyDown={e => { if (e.key === 'Escape' && !e.defaultPrevented && !(e.target as HTMLElement).closest('[role="dialog"]')) back() }}
      className={`${active ? 'flex' : 'hidden lg:flex'} min-w-0 flex-1 flex-col overflow-y-auto focus:outline-none`}>
      {active ? <><div className="lg:hidden px-4 pt-3"><button onClick={back} className="inline-flex items-center gap-1 text-content-secondary py-1"><ArrowLeft size={15} />返回资料列表</button></div>{children}</> : <div className="m-auto flex flex-col items-center gap-3 text-content-tertiary"><BookOpen size={28} /><span>选择一份资料查看详情</span></div>}
    </div>
  </div>
}

export function MaterialDetail({ material, busy, onAction, onOpenKnowledge, onReference }: {
  material: Material; busy: boolean; onAction: RunAction; onOpenKnowledge?: PanelProps['onOpenKnowledge']; onReference: (task: PersonalTask, ref: SourceRef) => void
}) {
  const source = materialSource(material)
  const state = source?.source.source_state as { state: number; validity: string; purpose: string } | undefined
  const results = materialResults(material)
  const original = material.originals[0]
  const reorganize = materialReorganizeSource(material)
  const current = results.some(item => item.ref.current_searchable === true)
  const latest = material.tasks[0]
  const reorganizeId = useRef(crypto.randomUUID())
  useEffect(() => { reorganizeId.current = crypto.randomUUID() }, [material.key, state?.state])
  return <article className="p-4 md:p-6 space-y-6 max-w-4xl w-full mx-auto">
    <header className="space-y-3">
      <div className="flex items-start justify-between gap-3"><h3 className="text-lg font-semibold text-content break-words">{material.title}</h3>
        {source && <SourceManager source={source.source} busy={busy} onAction={(name, value) => onAction(source.task, name, value)} />}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-content-secondary">
        {state && <span>{purposeLabels[state.purpose] || state.purpose} · {validityLabels[state.validity] || state.validity}</span>}
        <span className={current ? 'text-accent' : ''}>{materialKnowledgeLabel(material)}</span>
        <span className={latest.status === 'failed' ? 'text-danger' : ''}>最近处理：{statuses[latest.status] || latest.status}{latest.status === 'failed' && current ? '，旧知识仍有效' : ''}</span>
      </div>
      <div className="flex flex-wrap gap-2">
        {original && <a className={btn} href={apiClient.personalOriginal(original.task.task_id, original.attachment.attachment_id)} target="_blank" rel="noreferrer" title={original.attachment.filename}>打开原件 <ArrowUpRight size={13} className="inline" /></a>}
        {reorganize && state?.validity === 'active' && state.purpose === 'long_term' && <Btn disabled={busy} onClick={() => void onAction(reorganize.task, 'reorganize', { document_id: material.documentId!, expected_state: state.state, request_id: reorganizeId.current })}><RefreshCw size={13} className="inline mr-1.5" />重新整理</Btn>}
      </div>
    </header>
    <section aria-label="知识结果" className="space-y-3">
      <h4 className="text-sm font-semibold text-content flex items-center gap-2"><BookOpen size={15} className="text-accent" />知识结果 <span className="text-content-tertiary font-normal">{results.length}</span></h4>
      {!results.length && <div className="rounded-card border border-default bg-surface px-4 py-5 text-content-tertiary">尚无知识结果</div>}
      {results.map(({ task, ref }) => <KnowledgeResult key={String(ref.publish_id)} source={ref} onOpenKnowledge={onOpenKnowledge} onReference={() => onReference(task, ref)} />)}
    </section>
    {!material.recordOnly && <section aria-label="原件与视觉" className="space-y-3 border-t border-default pt-5">
      <h4 className="text-sm font-semibold text-content flex items-center gap-2"><FileText size={15} className="text-accent" />原件与视觉</h4>
      <div className="rounded-card bg-surface border border-default p-4 space-y-2">
        <div className="text-content-secondary" title={original?.attachment.filename || String(source?.source.filename || '')}>{readableFilename(original?.attachment.filename || source?.source.filename)}</div>
        {source ? <><VisualCoverage source={source.source} /><VisualWarnings source={source.source} /></> : <span className="text-xs text-content-tertiary">来源尚未登记</span>}
      </div>
    </section>}
    <section aria-label="处理历史" className="border-t border-default pt-5 space-y-3">
      <h4 className="text-sm font-semibold text-content flex items-center gap-2"><History size={15} className="text-accent" />处理历史 <span className="font-normal text-content-tertiary">{material.tasks.length}</span></h4>
      {material.tasks.map((task, index) => <TaskHistory key={task.task_id} task={task} busy={busy} latest={index === 0} onAction={onAction}
        historyResults={material.results.filter(item => item.task.task_id === task.task_id && !results.some(current => current.ref.publish_id === item.ref.publish_id)).map(item => item.ref)}
        onReference={ref => onReference(task, ref)} />)}
    </section>
  </article>
}

export function KnowledgeResult({ source, onReference, onOpenKnowledge }: { source: SourceRef; onReference: () => void; onOpenKnowledge?: PanelProps['onOpenKnowledge'] }) {
  return <div className="rounded-card border border-default bg-surface p-4 flex flex-wrap items-start gap-3 justify-between">
    <div className="min-w-0 flex-1 basis-48 space-y-1.5"><div className="font-medium text-content break-words">{readableFilename(source.title, '知识结果')}</div>
      <div className="flex flex-wrap gap-2 text-xs"><span className="text-content-tertiary">{String(source.category || '未分类')}</span><span className={source.current_searchable === true ? 'text-accent' : 'text-content-tertiary'}>{source.current_searchable === true ? '当前可检索' : '当前不可检索'}</span></div>
    </div>
    <div className="flex gap-2 flex-wrap justify-start min-w-0">
      {typeof source.path === 'string' && onOpenKnowledge && <Btn disabled={source.current_searchable !== true} onClick={() => onOpenKnowledge(source.path as string, String(source.title || '知识页'))}>查看知识页</Btn>}
      {typeof source.publish_id === 'string' && <Btn onClick={onReference}>查看出处</Btn>}
    </div>
  </div>
}

export function TaskHistory({ task, busy, latest, onAction, historyResults = [], onReference }: { task: PersonalTask; busy: boolean; latest: boolean; onAction: RunAction; historyResults?: SourceRef[]; onReference?: (ref: SourceRef) => void }) {
  const [input, setInput] = useState('')
  const actions = taskActions(task)
  const interactive = actions.length > 0
  return <details open={interactive ? true : undefined} className="rounded-card border border-default bg-surface px-4 py-3">
    <summary className="cursor-pointer text-content-secondary flex flex-wrap gap-2 items-center">
      <span className={task.status === 'failed' ? 'text-danger' : ''}>{latest ? '最近处理' : '历史处理'} · {statuses[task.status] || task.status}</span>
      <span className="text-xs text-content-tertiary">{new Date(task.updated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</span>
      <Tooltip label={`任务 ${task.task_id}；第 ${task.attempt} 次；配置 ${task.config_revision}；通知 ${task.notification}`}><span tabIndex={0} aria-label="处理记录信息"><Info size={13} /></span></Tooltip>
    </summary>
    <div className="pt-3 space-y-3">
      <div className="text-xs text-content-tertiary">{stages[task.stage] || task.stage}{task.outcome ? ` · ${personalOutcomeLabel(task)}` : ''}</div>
      <ReadingProgress task={task} />
      {task.error && <div role="alert" className="text-danger">{task.error.code === 'config_unavailable' ? '原配置不可用，可按当前配置重新处理。' : task.error.message || task.error.code}</div>}
      {task.status === 'waiting_input' && <label className="block text-content-secondary space-y-2"><span>{task.question || '补充资料用途'}</span><TextInput aria-label="补充用途" value={input} disabled={busy} onChange={e => setInput(e.target.value)} /></label>}
      <div className="flex gap-2 flex-wrap">{actions.map(name => <Btn key={name} disabled={busy || (name === 'update_input' && !input.trim())} onClick={() => void onAction(task, name, undefined, input)}>{({ resubmit: '按当前配置重新处理', retry: '重试', cancel: '取消', update_input: '补充用途' } as Record<string, string>)[name]}</Btn>)}</div>
      {historyResults.length > 0 && <div aria-label="历史知识结果" className="space-y-2">{historyResults.map(ref => <div key={String(ref.publish_id)} className="flex items-center justify-between gap-2 text-xs text-content-secondary">
        <span>{readableFilename(ref.title || ref.filename, ref.page_id ? '历史知识页' : '原文投影')} · 历史记录</span>
        {onReference && typeof ref.publish_id === 'string' && <Btn onClick={() => onReference(ref)}>查看历史出处</Btn>}
      </div>)}</div>}
      {task.response && <details><summary className="cursor-pointer text-content-tertiary">原始回复</summary><Markdown content={task.response} /></details>}
      {task.attachments.length > 0 && <details><summary className="cursor-pointer text-xs text-content-tertiary">本次附件</summary><div className="flex flex-wrap gap-2 pt-2">{task.attachments.map(attachment => <a className={btn} key={attachment.attachment_id} href={apiClient.personalOriginal(task.task_id, attachment.attachment_id)} target="_blank" rel="noreferrer" title={attachment.filename}>{readableFilename(attachment.filename)}</a>)}</div></details>}
    </div>
  </details>
}

const btn = 'px-3 py-1.5 rounded-btn border border-default text-sm disabled:opacity-50'

export function ReadingProgress({ task }: { task: PersonalTask }) {
  const progress = task.reading_progress
  if (!progress) return null
  if (progress.unavailable) return <div role="status" className="text-xs text-content-tertiary">阅读状态不可用 <span title="阅读台账无法核验，请刷新；不据此判定资料已读或任务完成。">?</span></div>
  const ranges = (values?: string[]) => {
    const groups = new Map<string, number[]>()
    const office: string[] = []
    for (const value of values || []) {
      const word = value.match(/^([^:]+):word:([^:]+):(\d+)$/)
      const slide = value.match(/^([^:]+):slide:(.+)$/)
      if (word) { office.push(`${word[1]}：Word阅读副本页 ${word[3]}`); continue }
      if (slide) { office.push(`${slide[1]}：幻灯片标识 ${slide[2]}`); continue }
      const split = value.lastIndexOf(':')
      const revision = value.slice(0, split), page = Number(value.slice(split + 1))
      if (split < 1 || !Number.isInteger(page) || page < 1) continue
      groups.set(revision, [...(groups.get(revision) || []), page])
    }
    return [...[...groups].map(([revision, pages]) => `${revision}：物理页 ${pages.sort((a, b) => a - b).join('、')}`), ...office].join('\n') || '无'
  }
  const partial = !!progress.published?.length && !!progress.remaining?.length
  const detail = `已送达：${ranges(progress.delivered)}\n待补读：${ranges(progress.pending)}\n结果未知：${ranges(progress.unknown)}\n已发布：${ranges(progress.published)}\n待整理发布：${ranges(progress.remaining)}\n送达只表示请求成功，不等于每处内容已理解。部分发布不代表整任务成功。`
  return <div aria-label="阅读覆盖" className="text-xs text-content-secondary flex flex-wrap gap-2">
    <span>已送达 {progress.delivered?.length || 0}/{progress.targets?.length || 0} 页</span>
    <span>待补读 {progress.pending?.length || 0} 页</span>
    <span>{partial ? '部分已发布' : progress.task_complete ? '整理完成' : '待完成整理'}</span>
    <span tabIndex={0} aria-label="阅读范围详情" title={detail}>?</span>
  </div>
}

export function observationLabel(reference: Pick<PersonalReference, 'origin' | 'type' | 'reading_id'>): string {
  if (reference.origin === 'model' || reference.reading_id) {
    return reference.type === 'ocr' ? '模型图中文字' : reference.type === 'visual_interpretation' ? '模型视觉解释' : '模型观察'
  }
  return '原文文字'
}

export function ReadingCopyLink({ reference, taskId, publishId }: { reference: PersonalReference; taskId: string; publishId: string }) {
  const physical = reference.location.physical_page
  const location = reference.location
  const batch = typeof physical === 'number' ? reference.reading_copy?.page_map.indexOf(physical)
    : reference.reading_copy?.source_page_map?.findIndex(item => item.kind === location.kind && (
      location.kind === 'slide' ? item.source_slide_id === location.source_slide_id
        : location.kind === 'word_reading_copy_page' && item.reading_copy_page === location.reading_copy_page))
  if (batch === undefined || batch < 0) return null
  const label = (typeof physical === 'number' ? `物理页 ${physical}` : locationLabel(location))
    + (Array.isArray(location.region) ? ' · 局部补读' : '')
  return <a className={btn} href={apiClient.personalReadingCopy(taskId, publishId, reference.ordinal, batch + 1)} target="_blank" rel="noreferrer"
    title={`${label}；本批副本第 ${batch + 1} 页`}>打开阅读副本 · {label}</a>
}
const validityLabels: Record<string, string> = { active: '有效', withdrawn: '已撤回', corrected: '已更正' }
const purposeLabels: Record<string, string> = { long_term: '长期资料', transient: '临时使用', waiting: '用途待补充' }
const visualReasons: Record<string, string> = { visual_not_requested: '尚未视觉整理', renderer_unavailable: '页面渲染不可用', render_failed: '页面渲染失败', render_budget_exceeded: '超出渲染预算', config_unavailable: '视觉配置不可用', vision_unavailable: '视觉模型不可用', vision_failed: '视觉处理失败', visual_budget_exceeded: '超出视觉预算', vision_empty: '未识别到可靠内容', vision_invalid_response: '视觉结果无法核验' }
function VisualCoverage({ source }: { source: Record<string, unknown> }) {
  const coverage = (Array.isArray(source.visual_coverage) ? source.visual_coverage : []) as { physical_page: number; status?: string; unprocessed_reason?: string }[]
  if (!coverage.length) return null
  const processed = coverage.filter(page => page.status === 'processed').length
  const partial = coverage.filter(page => page.status === 'partial').length
  const pending = coverage.filter(page => page.status !== 'processed' && page.status !== 'partial' && page.status !== 'blank').length
  const summary = [processed && `${processed}页已处理`, partial && `${partial}页待核验`, pending && `${pending}页待处理`].filter(Boolean).join(' · ')
  if (!summary) return null
  const detail = coverage.map(page => `物理页 ${page.physical_page}：${page.status === 'processed' ? '已处理' : page.status === 'partial' ? '待核验' : page.status === 'blank' ? '空白页' : '待处理'}${page.unprocessed_reason ? `（${visualReasons[page.unprocessed_reason] || page.unprocessed_reason}）` : ''}`).join('\n')
  return <div className="text-xs text-content-tertiary">视觉：{summary} <span title={detail}>?</span></div>
}
function VisualWarnings({ source }: { source: Record<string, unknown> }) {
  const warnings = (Array.isArray(source.visual_warnings) ? source.visual_warnings : []).flatMap(item => {
    if (typeof item === 'string') return [item]
    if (!item || typeof item !== 'object') return []
    const warning = item as Record<string, unknown>
    const kind = warning.kind === 'image' ? '图像' : warning.kind === 'chart' ? '图表' : '图像／图表'
    const part = typeof warning.part === 'string' ? `（${warning.part}）` : ''
    const reason = warning.reason === 'visual_not_supported' ? '暂不支持视觉解析' : '尚未完成视觉解析'
    return [`${kind}待处理${part}：${reason}`]
  })
  return warnings.length ? <span className="text-xs text-content-tertiary" title={warnings.join('\n')}>图像／图表待处理 <span>?</span></span> : null
}
export function SourceControls({ source, busy, onAction, onReference, onOpenKnowledge }: {
  source: Record<string, unknown>; busy: boolean
  onAction: (name: string, source: PersonalSourceAction) => Promise<void>
  onReference: () => void
  onOpenKnowledge?: (path: string, title: string) => void
}) {
  const sources = (Array.isArray(source.sources) ? source.sources : []) as Record<string, unknown>[]
  const unique = [...new Map(sources.map(item => [`${item.document_id}:${item.revision_id}`, item])).values()]
  if (!unique.length) return <SourceEntryControls source={source} busy={busy} onAction={onAction} onReference={onReference} onOpenKnowledge={onOpenKnowledge} />
  return <section className="border-t border-default pt-3 space-y-2">
    <div className="text-sm"><strong>{String(source.title || '知识页')}</strong> · {String(source.category || '未分类')} · {source.current_searchable === true ? '当前可检索' : '当前不可检索'}</div>
    <VisualWarnings source={source} />
    <div className="flex gap-2 flex-wrap">
      {typeof source.path === 'string' && onOpenKnowledge && <button className={btn} disabled={source.current_searchable !== true} onClick={() => onOpenKnowledge(source.path as string, String(source.title || '知识页'))}>查看知识页</button>}
      {typeof source.publish_id === 'string' && <button className={btn} onClick={onReference}>查看出处</button>}
    </div>
    {unique.map(item => <SourceEntryControls key={`${item.document_id}:${item.revision_id}`} source={item} busy={busy} onAction={onAction} sourceOnly />)}
  </section>
}

function SourceEntryControls({ source, busy, onAction, onReference, onOpenKnowledge, sourceOnly = false }: {
  source: SourceRef; busy: boolean
  onAction: (name: string, source: PersonalSourceAction) => Promise<void | boolean>
  onReference?: () => void
  onOpenKnowledge?: PanelProps['onOpenKnowledge']
  sourceOnly?: boolean
}) {
  const filename = typeof source.filename === 'string' ? source.filename : ''
  const title = String((sourceOnly ? source.filename : source.title) || source.filename || '资料原文')
  const state = source.source_state as { validity: string; purpose: string } | null
  return <section className="border-t border-default pt-3 space-y-2">
    <div className="text-xs text-content-tertiary"><strong title={isTransportFilename(filename) || isTransportFilename(title) ? filename || title : undefined}>{isTransportFilename(filename) || isTransportFilename(title) ? '来源原件' : title}</strong>{!sourceOnly && ` · ${String(source.category || '未分类')}`}{state && ` · ${purposeLabels[state.purpose] || state.purpose} · ${validityLabels[state.validity] || state.validity}`}{!sourceOnly && ` · ${source.current_searchable === true ? '当前可检索' : '当前不可检索'}`}</div>
    <VisualWarnings source={source} /><VisualCoverage source={source} />
    <div className="flex gap-2 flex-wrap">
      {typeof source.page_id === 'string' && typeof source.path === 'string' && onOpenKnowledge && <Btn disabled={source.current_searchable !== true} onClick={() => onOpenKnowledge(source.path as string, String(source.title || '知识页'))}>查看知识页</Btn>}
      {typeof source.publish_id === 'string' && onReference && <Btn onClick={onReference}>查看出处</Btn>}
      <SourceManager source={source} busy={busy} onAction={onAction} />
    </div>
  </section>
}

export function PurposeSelect({ value, disabled, onChange }: { value: string; disabled: boolean; onChange: (value: 'long_term' | 'transient') => void }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const options = [{ value: 'long_term' as const, label: '长期资料' }, { value: 'transient' as const, label: '临时使用' }]
  useEffect(() => {
    if (!open) return
    root.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]')?.focus()
    const outside = (event: MouseEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', outside)
    return () => document.removeEventListener('mousedown', outside)
  }, [open])
  return <div ref={root} className="relative" onKeyDown={event => {
    if (event.key === 'Escape' && open) { event.stopPropagation(); event.preventDefault(); setOpen(false); trigger.current?.focus() }
    if (open && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault()
      const buttons = [...(root.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') || [])]
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
      buttons[event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowUp' ? -1 : 1) + buttons.length) % buttons.length]?.focus()
    }
  }}>
    <button ref={trigger} type="button" aria-label="修订资料用途" aria-haspopup="listbox" aria-expanded={open} disabled={disabled}
      onClick={() => setOpen(!open)} onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true) } }}
      className="w-full flex items-center justify-between px-3 py-2 rounded-btn border border-strong bg-inset text-content disabled:opacity-50">{purposeLabels[value] || value}<ChevronDown size={14} /></button>
    {open && <div role="listbox" aria-label="资料用途选项" className="absolute z-[70] left-0 right-0 top-full mt-1 p-1 rounded-btn border border-default bg-elevated shadow-lg">
      {options.map(option => <button key={option.value} type="button" role="option" aria-selected={value === option.value}
        onClick={() => { onChange(option.value); setOpen(false); trigger.current?.focus() }}
        className={`w-full text-left rounded-btn px-3 py-2 focus:bg-accent-soft focus:text-accent ${value === option.value ? 'text-accent bg-accent-soft' : 'text-content-secondary hover:bg-surface-2'}`}>{option.label}</button>)}
    </div>}
  </div>
}

export function SourceManagementForm({ source, busy, reason, purpose, onReason, onPurpose, onSubmit, onClose }: {
  source: SourceRef; busy: boolean; reason: string; purpose: 'long_term' | 'transient'; onReason: (value: string) => void;
  onPurpose: (value: 'long_term' | 'transient') => void; onSubmit: (name: string) => void; onClose: () => void
}) {
  const state = source.source_state as { state: number; purpose: string; validity: string; current?: string | null } | null
  const [operation, setOperation] = useState('')
  if (!state || typeof source.document_id !== 'string') return null
  const operations = [
    { value: 'change_purpose', label: '修改用途' },
    ...(state.validity !== 'corrected' ? [{ value: 'correct', label: '标记内容有误' }] : []),
    ...(state.validity === 'active' ? [{ value: 'withdraw', label: '撤回资料' }] : []),
    ...(!state.current || state.validity !== 'active' ? [{ value: 'republish', label: '重新长期发布' }] : []),
  ]
  const selected = operations.find(item => item.value === operation)
  return <div className="space-y-4">
    <div className="flex items-center justify-between text-content-secondary"><span title={String(source.filename || '')}>{readableFilename(source.filename)}</span>
      <Tooltip label="更正、撤回或改为临时会暂停所有引用该资料的知识页及其派生内容；恢复长期用途不会自动恢复旧页。"><button type="button" aria-label="来源变更影响" className="p-1 text-content-tertiary"><Info size={15} /></button></Tooltip>
    </div>
    <div role="group" aria-label="选择来源操作" className="flex flex-wrap gap-2">{operations.map(item => <button key={item.value} type="button" aria-pressed={operation === item.value} disabled={busy}
      onClick={() => setOperation(item.value)} className={`px-3 py-2 rounded-btn border text-sm transition-colors ${operation === item.value ? 'border-accent bg-accent-soft text-accent' : 'border-default text-content-secondary hover:bg-surface-2'}`}>{item.label}</button>)}</div>
    {selected && <div className="space-y-4 border-t border-default pt-4">
      {operation === 'change_purpose' && <PurposeSelect value={purpose} disabled={busy} onChange={onPurpose} />}
      {operation !== 'withdraw' && <label className="block space-y-2 text-content-secondary"><span>{operation === 'correct' ? '内容有误的依据' : operation === 'republish' ? '重新发布依据' : '用途修订依据'}</span><TextInput autoFocus aria-label="更正或用途修订依据" disabled={busy} value={reason} onChange={e => onReason(e.target.value)} /></label>}
      <div className="flex justify-end"><Btn variant={operation === 'withdraw' ? 'danger' : 'primary'} disabled={busy || (operation !== 'withdraw' && !reason.trim()) || (operation === 'change_purpose' && purpose === state.purpose)} onClick={() => onSubmit(operation)}>
        {operation === 'change_purpose' ? '保存用途' : selected.label}
      </Btn></div>
    </div>}
  </div>
}

export function SourceManager({ source, busy, onAction }: { source: SourceRef; busy: boolean; onAction: (name: string, source: PersonalSourceAction) => Promise<void | boolean> }) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const state = source.source_state as { state: number; purpose: string } | undefined
  const [purpose, setPurpose] = useState<'long_term' | 'transient'>(state?.purpose === 'transient' ? 'transient' : 'long_term')
  const requestId = useRef(crypto.randomUUID())
  const trigger = useRef<HTMLButtonElement>(null)
  const body = useRef<HTMLDivElement>(null)
  const close = useCallback(() => { setOpen(false); trigger.current?.focus() }, [])
  useEffect(() => { requestId.current = crypto.randomUUID() }, [state?.state, reason, purpose])
  useEffect(() => {
    if (!open) return
    const surface = body.current?.parentElement?.parentElement
    const initialFocus = body.current?.querySelector<HTMLButtonElement>('[aria-label="选择来源操作"] button:not(:disabled)')
      || surface?.querySelector<HTMLButtonElement>('button:not(:disabled)')
    initialFocus?.focus()
    const key = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); if (!busy) close() }
      if (event.key === 'Tab') {
        const focusable = [...(surface?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),[tabindex="0"]') || [])]
        const first = focusable[0], last = focusable[focusable.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [open, busy, close])
  if (!state || !Number.isInteger(state.state) || typeof source.document_id !== 'string') return null
  const dispatch = async (name: string) => {
    if (busy) return
    const result = await onAction(name, { document_id: source.document_id as string, expected_state: state.state,
      reason: name === 'withdraw' ? undefined : reason.trim(), purpose, request_id: requestId.current })
    if (result !== false) close()
  }
  return <>
    <button ref={trigger} type="button" disabled={busy} onClick={() => { setOpen(true); setPurpose(state.purpose === 'transient' ? 'transient' : 'long_term') }}
      className="inline-flex items-center gap-1.5 text-sm text-content-secondary hover:text-accent px-2 py-2 rounded-btn hover:bg-accent-soft disabled:opacity-50"><Settings2 size={14} />来源管理</button>
    <Modal open={open} title="来源管理" onClose={() => { if (!busy) close() }}>
      <div ref={body} role="dialog" aria-modal="true" aria-label="来源管理" onKeyDown={event => {
        if (event.key === 'Escape' && !event.defaultPrevented) { event.preventDefault(); event.stopPropagation(); if (!busy) close() }
      }}>
        <SourceManagementForm source={source} busy={busy} reason={reason} purpose={purpose} onReason={setReason} onPurpose={setPurpose} onSubmit={name => void dispatch(name)} onClose={close} />
      </div>
    </Modal>
  </>
}

function locationLabel(location: Record<string, unknown>): string {
  if (location.kind === 'word_reading_copy_page') return `Word阅读副本页 ${String(location.reading_copy_page)}`
  if (location.kind === 'slide') return `幻灯片 ${String(location.source_slide_number)} · 标识 ${String(location.source_slide_id)}`
  if (location.kind === 'docx' && typeof location.path === 'string') {
    const names: Record<string, string> = { paragraph: '段落', table: '表格', row: '行', cell: '单元格', control: '内容控件' }
    return location.path.split('/').map(part => part === 'body' ? '正文' : part.replace(/^(paragraph|table|row|cell|control)\[(\d+)\]$/, (_, kind: string, number: string) => `${names[kind]} ${number}`)).join(' · ')
  }
  const labels: Record<string, string> = { page: '物理页', physical_page: '物理页', sheet: '工作表', cell: '单元格', range: '区域', paragraph: '段落', paragraph_start: '起始段落', paragraph_end: '结束段落', section: '章节', table: '表格', row: '行', column: '列', line: '行', line_start: '起始行', line_end: '结束行' }
  return Object.entries(location).filter(([key, value]) => key in labels && value != null).map(([key, value]) => `${labels[key]} ${Array.isArray(value) ? value.join('、') : typeof value === 'object' ? locationLabel(value as Record<string, unknown>) : String(value)}`).join(' · ') || '原文位置'
}

function ReferenceBrowser({ taskId, publishId, version, onClose }: { taskId: string; publishId: string; version: string; onClose: () => void }) {
  const [reference, setReference] = useState<PersonalReference | null>(null)
  const [ordinal, setOrdinal] = useState(0)
  const [jump, setJump] = useState('1')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  useEffect(() => { setOrdinal(0); setJump('1'); setReference(null) }, [taskId, publishId])
  useEffect(() => {
    let active = true
    setLoading(true); setError('')
    apiClient.personalReference(taskId, publishId, ordinal).then(res => {
      if (!active) return
      if (res.status !== 'success') throw new Error(res.message || '读取出处失败')
      setReference(res.reference); setJump(String(res.reference.ordinal + 1))
    }).catch(e => { if (active) setError(e.message) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [taskId, publishId, ordinal, version])
  const count = reference?.unit_count || 0
  return <section className="rounded-lg border border-default p-4 space-y-3">
    <div className="flex gap-2 items-center flex-wrap"><button className={btn} onClick={onClose}>关闭出处</button>
      <button className={btn} disabled={loading || ordinal <= 0} onClick={() => setOrdinal(v => v - 1)}>上一处</button>
      <input aria-label="出处位置序号" type="number" min={1} max={count || 1} className="w-24 bg-inset border border-default rounded-btn px-2 py-1" value={jump} onChange={e => setJump(e.target.value)} />
      <span>/ {count}</span><button className={btn} disabled={loading || !Number.isInteger(Number(jump)) || Number(jump) < 1 || Number(jump) > count} onClick={() => setOrdinal(Number(jump) - 1)}>跳转</button>
      <button className={btn} disabled={loading || ordinal + 1 >= count} onClick={() => setOrdinal(v => v + 1)}>下一处</button>
      <span title="按已发布知识的出处目录跳转；原文物理页、段落或单元格见位置标签。">?</span>
    </div>
    {error && <div role="alert" className="text-danger">{error}</div>}
    {loading ? <div role="status">读取中…</div> : reference && <>
      <div className="text-sm">{reference.filename || reference.title || '资料原文'} · {reference.current_status === 'current' ? '当前有效' : reference.current_status === 'withdrawn' ? '已撤回' : reference.current_status === 'corrected' ? '已更正' : '已替代或存在未发布变更'} · {locationLabel(reference.location)}</div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-content-secondary"><span>{observationLabel(reference)}</span>
        {reference.origin === 'model' && <span title="模型生成的转写或解释，可打开阅读副本核对；不作为原文文字层显示。">?</span>}
        <ReadingCopyLink reference={reference} taskId={taskId} publishId={publishId} />
      </div>
      <details><summary className="cursor-pointer text-sm">出处目录</summary><div className="flex flex-wrap gap-2 py-2">{reference.units.slice(Math.floor(ordinal / 50) * 50, Math.floor(ordinal / 50) * 50 + 50).map(unit => <button key={unit.ordinal} className={btn} onClick={() => setOrdinal(unit.ordinal)} aria-current={unit.ordinal === ordinal}>{unit.filename ? `${unit.filename} · ` : ''}{locationLabel(unit.location)}</button>)}</div></details>
      <Markdown figures={{ ...reference, publish_id: publishId }} figureView="history" content={reference.text} />
    </>}
  </section>
}
