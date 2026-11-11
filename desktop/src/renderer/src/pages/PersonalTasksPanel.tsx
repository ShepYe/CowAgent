import Markdown from '../components/Markdown'
import React, { useCallback, useEffect, useRef, useState } from 'react'
import { askConfirm } from '../store/confirmStore'
import apiClient, { type PersonalTask, type PersonalSourceAction, type PersonalReference } from '../api/client'

const statuses: Record<string, string> = { queued: '已接收', running: '处理中', waiting_input: '等待补充', succeeded: '已完成', failed: '失败', cancelled: '已取消', needs_reconcile: '待核对' }
const stages: Record<string, string> = { receive: '接收', download: '保存原件', decide: '判断用途', parse: '解析', publish: '发布', pending_index: '已保存，待索引', done: '完成' }
const outcomes: Record<string, string> = { transient: '临时使用', duplicate: '重复资料' }
export function personalOutcomeLabel(task: PersonalTask): string {
  if (task.outcome !== 'published') return outcomes[task.outcome || ''] || task.outcome || ''
  const publications = task.result_refs.filter(ref => typeof ref.publish_id === 'string')
  const verified = publications.filter(ref => ref.current_searchable === true).length
  return !verified ? '已发布（当前不可检索）' : verified === publications.length ? '当前可检索' : '部分可检索'
}
const canResubmit = (task: PersonalTask) => task.status === 'failed' && task.error?.code === 'config_unavailable' && task.attachments.length > 0 && !task.result_refs.length && !task.payload?.source_action

function isTransportFilename(value: string): boolean {
  const name = value.replace(/\\/g, '/').split('/').pop() || ''
  return /^file_v\d+_/i.test(name) || /^(?:[a-f0-9]{32}|[a-f0-9]{64})(?:\.[^.]+)?$/i.test(name)
}

export function personalTaskTitle(task: PersonalTask): string {
  const title = task.result_refs.find(ref => typeof ref.title === 'string' && ref.title.trim() && !isTransportFilename(ref.title))?.title
  if (typeof title === 'string') return title
  const filenames = task.attachments.map(item => item.filename).filter(Boolean)
  if (!filenames.length && task.payload?.filename) filenames.push(task.payload.filename)
  return filenames.map(name => isTransportFilename(name) ? '资料处理任务' : name).join('、') || '知识整理任务'
}

export function personalKnowledgeFingerprint(tasks: PersonalTask[]): string {
  return JSON.stringify(tasks.flatMap(task => task.result_refs
    .filter(ref => typeof ref.publish_id === 'string')
    .map(ref => JSON.stringify([task.task_id, task.status, ref.page_id, ref.page_version, ref.publish_id,
      ref.current_searchable === true, ref.title, ref.category, ref.path]))).sort())
}

export default function PersonalTasksPanel({ onOpenKnowledge, onKnowledgeChanged }: {
  onOpenKnowledge?: (path: string, title: string) => void
  onKnowledgeChanged?: () => void
}) {
  const [tasks, setTasks] = useState<PersonalTask[]>([])
  const [reference, setReference] = useState<{ taskId: string; publishId: string } | null>(null)
  const [referenceVersion, setReferenceVersion] = useState(0)
  const [message, setMessage] = useState('')
  const [success, setSuccess] = useState('')
  const [busy, setBusy] = useState('')
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const requestId = useRef(crypto.randomUUID())
  const resubmitRequestIds = useRef(new Map<string, string>())
  const fileInput = useRef<HTMLInputElement>(null)
  const knowledgeFingerprint = useRef<string | null>(null)
  const knowledgeChangedCallback = useRef(onKnowledgeChanged)
  useEffect(() => { knowledgeChangedCallback.current = onKnowledgeChanged }, [onKnowledgeChanged])
  const load = useCallback(async () => {
    try {
      const res = await apiClient.personalTasks()
      if (res.status !== 'success') throw new Error(res.message || '读取任务失败')
      setTasks(res.tasks)
      const fingerprint = personalKnowledgeFingerprint(res.tasks)
      if (knowledgeFingerprint.current !== fingerprint) {
        knowledgeFingerprint.current = fingerprint
        knowledgeChangedCallback.current?.()
      }
    } catch (e) { setMessage(e instanceof Error ? e.message : '读取任务失败') }
  }, [])
  useEffect(() => {
    void load()
    const timer = setInterval(() => void load(), 4000)
    return () => clearInterval(timer)
  }, [load])
  const submit = async () => {
    if (!file || busy) return
    if (file.size > 100 * 1024 * 1024) { setMessage('文件超过 100 MiB 上限'); return }
    setBusy('submit'); setMessage(''); setSuccess('')
    try {
      const res = await apiClient.submitPersonal(file, text, requestId.current)
      if (res.status !== 'success') throw new Error(res.message || '提交失败')
      setFile(null); setText(''); requestId.current = crypto.randomUUID()
      if (fileInput.current) fileInput.current.value = ''
      await load()
    } catch (e) { setMessage(e instanceof Error ? e.message : '提交失败') }
    finally { setBusy('') }
  }
  const action = async (task: PersonalTask, name: string, source?: PersonalSourceAction) => {
    if (busy || (name === 'resubmit' && !canResubmit(task))) return
    if (name === 'withdraw' && !(await askConfirm({ titleKey: 'personal_withdraw', msgKey: 'personal_withdraw_confirm', okKey: 'personal_withdraw' }))) return
    setBusy(task.task_id); setMessage(''); setSuccess('')
    try {
      let resubmitRequestId: string | undefined
      if (name === 'resubmit') {
        const key = `${task.task_id}:${task.attempt}`
        resubmitRequestId = resubmitRequestIds.current.get(key) ?? crypto.randomUUID()
        resubmitRequestIds.current.set(key, resubmitRequestId)
      }
      const res = await apiClient.personalAction(task, name, inputs[task.task_id], source, resubmitRequestId)
      if (res.status !== 'success') throw new Error(res.message || '操作失败')
      setSuccess(name === 'resubmit' ? '已创建新任务，使用当前配置处理原附件，旧任务记录已保留。' : '操作成功')
      setReferenceVersion(v => v + 1)
    } catch (e) { setMessage(e instanceof Error ? e.message : '操作失败') }
    finally { await load(); setBusy('') }
  }
  const button = 'px-3 py-1.5 rounded-btn border border-default text-sm disabled:opacity-50'
  return <div className="flex-1 overflow-auto border-t border-default p-6 space-y-4">
    <div className="flex flex-wrap gap-2 items-center">
      <input ref={fileInput} aria-label="选择资料" type="file" accept=".pdf,.docx,.xlsx,.md,.txt" disabled={!!busy} onChange={e => { setFile(e.target.files?.[0] || null); requestId.current = crypto.randomUUID() }} />
      <input className="bg-inset border border-default rounded-btn px-3 py-2 flex-1" placeholder="补充用途" aria-label="资料用途" value={text} disabled={!!busy} onChange={e => { setText(e.target.value); requestId.current = crypto.randomUUID() }} />
      <span title="正常分享后由助手自主整理；仅临时使用时请在用途里说明。支持 PDF、DOCX、XLSX、MD、TXT，单件不超过 100 MiB。">?</span>
      <button className={button} disabled={!file || !!busy} onClick={() => void submit()}>{busy === 'submit' ? '接收中…' : '发送资料'}</button>
      <button className={button} onClick={() => void load()}>刷新</button>
    </div>
    {reference && <ReferenceBrowser taskId={reference.taskId} publishId={reference.publishId} version={`${referenceVersion}:${JSON.stringify(tasks.find(t => t.task_id === reference.taskId)?.result_refs.map(r => [r.current_searchable, r.source_state, (r.sources as Record<string, unknown>[] | undefined)?.map(s => s.source_state)]))}`} onClose={() => setReference(null)} />}
    {message && <div role="alert" className="text-danger text-sm">{message}</div>}
    {success && <div role="status" className="text-sm">{success}</div>}
    {!tasks.length && <div className="text-content-tertiary text-sm">暂无资料任务</div>}
    {tasks.map(task => <article key={task.task_id} className="rounded-lg border border-default p-4 space-y-2">
      <div className="flex justify-between gap-3"><strong>{personalTaskTitle(task)}</strong><span>{statuses[task.status] || task.status} · {stages[task.stage] || task.stage}{task.outcome ? ` · ${personalOutcomeLabel(task)}` : ''}</span></div>
      <div className="text-xs text-content-tertiary" title={`任务 ${task.task_id}\n配置 ${task.config_revision}\n通知 ${task.notification}`}>第 {task.attempt} 次 · {new Date(task.updated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })} <span>?</span></div>
      {task.error && <div role="alert" className="text-danger text-sm">{task.error.code === 'config_unavailable' ? '任务原配置不可用，无法继续使用原配置处理。' : `${task.error.code}${task.error.message ? ` · ${task.error.message}` : ''}`}</div>}
      {task.response && <details><summary className="cursor-pointer text-sm text-content-secondary">处理记录</summary><Markdown content={task.response} /></details>}

      {task.status === 'waiting_input' && <div className="flex gap-2"><label className="flex-1">{task.question}<input aria-label="补充用途" className="w-full bg-inset border border-default rounded-btn px-3 py-2" value={inputs[task.task_id] || ''} onChange={e => setInputs({ ...inputs, [task.task_id]: e.target.value })} /></label><button className={button} disabled={!!busy || !inputs[task.task_id]?.trim()} onClick={() => void action(task, 'update_input')}>补充用途</button></div>}
      <div className="flex gap-2 flex-wrap">
        {canResubmit(task) ? <button className={button} disabled={!!busy} title="复用原附件，按当前配置创建新任务；旧任务及其记录会保留。请求失败后可再次点击。" onClick={() => void action(task, 'resubmit')}>按当前配置重新处理</button> : task.status === 'failed' && task.error?.code !== 'config_unavailable' && task.error?.retryable && <button className={button} disabled={!!busy} onClick={() => void action(task, 'retry')}>重试</button>}
        {['queued', 'running', 'waiting_input'].includes(task.status) && <button className={button} disabled={!!busy} onClick={() => void action(task, 'cancel')}>{busy === task.task_id ? '处理中…' : '取消'}</button>}

        {task.attachments.map(a => <a className={button} key={a.attachment_id} href={apiClient.personalOriginal(task.task_id, a.attachment_id)} target="_blank" rel="noreferrer" title={a.filename}>打开原件</a>)}
      </div>
      {task.result_refs.map((ref, i) => <SourceControls key={`${String(ref.document_id)}:${i}`} source={ref} busy={!!busy} onAction={(name, source) => action(task, name, source)} onOpenKnowledge={onOpenKnowledge} onReference={() => setReference({ taskId: task.task_id, publishId: ref.publish_id as string })} />)}
    </article>)}
  </div>
}

const btn = 'px-3 py-1.5 rounded-btn border border-default text-sm disabled:opacity-50'
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
  source: Record<string, unknown>; busy: boolean
  onAction: (name: string, source: PersonalSourceAction) => Promise<void>
  onReference?: () => void
  onOpenKnowledge?: (path: string, title: string) => void
  sourceOnly?: boolean
}) {
  const state = source.source_state as { state: number; validity: string; purpose: string; current: string | null } | null
  const [reason, setReason] = useState('')
  const [purpose, setPurpose] = useState<'long_term' | 'transient'>('long_term')
  const requestId = useRef(crypto.randomUUID())
  const documentId = typeof source.document_id === 'string' ? source.document_id : ''
  const filename = typeof source.filename === 'string' ? source.filename : ''
  const sourceTitle = String((sourceOnly ? source.filename : source.title) || source.filename || '资料原文')
  const transportName = isTransportFilename(filename) || isTransportFilename(sourceTitle)
  const dispatch = async (name: string) => {
    if (!state) return
    await onAction(name, { document_id: documentId, expected_state: state.state, reason: name === 'withdraw' ? undefined : reason.trim(), purpose, request_id: requestId.current })
  }
  useEffect(() => { requestId.current = crypto.randomUUID() }, [state?.state])
  return <section className="border-t border-default pt-3 space-y-2">
    <div className="text-xs text-content-tertiary"><strong title={transportName ? filename || sourceTitle : undefined}>{transportName ? '来源原件' : sourceTitle}</strong>{!sourceOnly && ` · ${String(source.category || '未分类')}`}{state && ` · ${purposeLabels[state.purpose] || state.purpose} · ${validityLabels[state.validity] || state.validity}`}{sourceOnly ? ` · ${source.source_status === 'current' ? '来源当前有效' : source.source_status === 'superseded_or_changed' ? '来源版本或状态已变化' : source.source_status === 'unavailable' ? '来源待核对' : ''}` : ` · ${source.current_searchable === true ? '当前可检索' : '当前不可检索'}`}</div>
    <VisualWarnings source={source} />
    <VisualCoverage source={source} />
    <div className="flex gap-2 flex-wrap">
      {typeof source.page_id === 'string' && typeof source.path === 'string' && onOpenKnowledge && <button className={btn} disabled={source.current_searchable !== true} onClick={() => onOpenKnowledge(source.path as string, String(source.title || '知识页'))}>查看知识页</button>}
      {state?.validity === 'active' && state.purpose === 'long_term' && documentId && <button className={btn} disabled={busy} title="复用原件，使用当前配置创建模型整理任务；新发布成功前保留旧有效结果。" onClick={() => void dispatch('reorganize')}>重新整理</button>}
      {typeof source.publish_id === 'string' && onReference && <button className={btn} onClick={onReference}>查看出处</button>}
      {state && documentId && <>
        <input className="flex-1 bg-inset border border-default rounded-btn px-3 py-2" aria-label="更正或用途修订依据" placeholder="更正或用途修订依据" disabled={busy} value={reason} onChange={e => { setReason(e.target.value); requestId.current = crypto.randomUUID() }} />
        <span title="更正或用途变化会立即屏蔽旧引用。改为长期不会自动重新发布；重新长期发布会创建独立任务，成功后才可检索。">?</span>
        {state.validity !== 'corrected' && <button className={btn} disabled={busy || !reason.trim()} onClick={() => void dispatch('correct')}>登记更正</button>}
        <select aria-label="修订资料用途" className="bg-inset border border-default rounded-btn px-2" disabled={busy} value={purpose} onChange={e => setPurpose(e.target.value as 'long_term' | 'transient')}><option value="long_term">长期资料</option><option value="transient">临时使用</option></select>
        <button className={btn} disabled={busy || !reason.trim() || purpose === state.purpose} onClick={() => void dispatch('change_purpose')}>修订用途</button>
        {(!state.current || state.validity !== 'active') && <button className={btn} disabled={busy || !reason.trim()} onClick={() => void dispatch('republish')}>重新长期发布</button>}
        {state.validity === 'active' && <button className={btn} disabled={busy} onClick={() => void dispatch('withdraw')}>撤回</button>}
      </>}
    </div>
  </section>
}

function locationLabel(location: Record<string, unknown>): string {
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
      <details><summary className="cursor-pointer text-sm">出处目录</summary><div className="flex flex-wrap gap-2 py-2">{reference.units.slice(Math.floor(ordinal / 50) * 50, Math.floor(ordinal / 50) * 50 + 50).map(unit => <button key={unit.ordinal} className={btn} onClick={() => setOrdinal(unit.ordinal)} aria-current={unit.ordinal === ordinal}>{unit.filename ? `${unit.filename} · ` : ''}{locationLabel(unit.location)}</button>)}</div></details>
      <Markdown content={reference.text} />
    </>}
  </section>
}
