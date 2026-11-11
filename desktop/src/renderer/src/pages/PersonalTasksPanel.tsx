import Markdown from '../components/Markdown'
import React, { useCallback, useEffect, useRef, useState } from 'react'
import { askConfirm } from '../store/confirmStore'
import apiClient, { type PersonalTask, type PersonalSourceAction, type PersonalReference } from '../api/client'

const statuses: Record<string, string> = { queued: '已接收', running: '处理中', waiting_input: '等待补充', succeeded: '已完成', failed: '失败', cancelled: '已取消', needs_reconcile: '待核对' }
const stages: Record<string, string> = { receive: '接收', download: '保存原件', decide: '判断用途', parse: '解析', publish: '发布', pending_index: '已保存，待索引', done: '完成' }
const outcomes: Record<string, string> = { published: '可检索', transient: '临时使用', duplicate: '重复资料' }
const canResubmit = (task: PersonalTask) => task.status === 'failed' && task.error?.code === 'config_unavailable' && task.attachments.length > 0 && !task.result_refs.length && !task.payload?.source_action

export default function PersonalTasksPanel() {
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
  const load = useCallback(async () => {
    try {
      const res = await apiClient.personalTasks()
      if (res.status !== 'success') throw new Error(res.message || '读取任务失败')
      setTasks(res.tasks)
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
    {reference && <ReferenceBrowser taskId={reference.taskId} publishId={reference.publishId} version={`${referenceVersion}:${JSON.stringify(tasks.find(t => t.task_id === reference.taskId)?.result_refs.map(r => r.source_state))}`} onClose={() => setReference(null)} />}
    {message && <div role="alert" className="text-danger text-sm">{message}</div>}
    {success && <div role="status" className="text-sm">{success}</div>}
    {!tasks.length && <div className="text-content-tertiary text-sm">暂无资料任务</div>}
    {tasks.map(task => <article key={task.task_id} className="rounded-lg border border-default p-4 space-y-2">
      <div className="flex justify-between gap-3"><strong>{task.attachments.map(a => a.filename).join('、') || task.payload?.filename || task.task_id}</strong><span>{statuses[task.status] || task.status} · {stages[task.stage] || task.stage}{task.outcome ? ` · ${task.outcome === 'published' && !task.result_refs.some(ref => ref.current_searchable) ? '历史发布（当前不可检索）' : outcomes[task.outcome] || task.outcome}` : ''}</span></div>
      <div className="text-xs text-content-tertiary" title={`任务 ${task.task_id}\n配置 ${task.config_revision}\n通知 ${task.notification}`}>第 {task.attempt} 次 · {new Date(task.updated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })} <span>?</span></div>
      {task.error && <div role="alert" className="text-danger text-sm">{task.error.code === 'config_unavailable' ? '任务原配置不可用，无法继续使用原配置处理。' : `${task.error.code}${task.error.message ? ` · ${task.error.message}` : ''}`}</div>}
      {task.response && <Markdown content={task.response} />}

      {task.status === 'waiting_input' && <div className="flex gap-2"><label className="flex-1">{task.question}<input aria-label="补充用途" className="w-full bg-inset border border-default rounded-btn px-3 py-2" value={inputs[task.task_id] || ''} onChange={e => setInputs({ ...inputs, [task.task_id]: e.target.value })} /></label><button className={button} disabled={!!busy || !inputs[task.task_id]?.trim()} onClick={() => void action(task, 'update_input')}>补充用途</button></div>}
      <div className="flex gap-2 flex-wrap">
        {canResubmit(task) ? <button className={button} disabled={!!busy} title="复用原附件，按当前配置创建新任务；旧任务及其记录会保留。请求失败后可再次点击。" onClick={() => void action(task, 'resubmit')}>按当前配置重新处理</button> : task.status === 'failed' && task.error?.code !== 'config_unavailable' && task.error?.retryable && <button className={button} disabled={!!busy} onClick={() => void action(task, 'retry')}>重试</button>}
        {['queued', 'running', 'waiting_input'].includes(task.status) && <button className={button} disabled={!!busy} onClick={() => void action(task, 'cancel')}>{busy === task.task_id ? '处理中…' : '取消'}</button>}

        {task.attachments.map(a => <a className={button} key={a.attachment_id} href={apiClient.personalOriginal(task.task_id, a.attachment_id)} target="_blank" rel="noreferrer" title={a.filename}>打开原件</a>)}
      </div>
      {task.result_refs.map((ref, i) => <SourceControls key={`${String(ref.document_id)}:${i}`} source={ref} busy={!!busy} onAction={(name, source) => action(task, name, source)} onReference={() => setReference({ taskId: task.task_id, publishId: ref.publish_id as string })} />)}
    </article>)}
  </div>
}

const btn = 'px-3 py-1.5 rounded-btn border border-default text-sm disabled:opacity-50'
const validityLabels: Record<string, string> = { active: '有效', withdrawn: '已撤回', corrected: '已更正' }
const purposeLabels: Record<string, string> = { long_term: '长期资料', transient: '临时使用', waiting: '用途待补充' }
function SourceControls({ source, busy, onAction, onReference }: {
  source: Record<string, unknown>; busy: boolean
  onAction: (name: string, source: PersonalSourceAction) => Promise<void>
  onReference: () => void
}) {
  const state = source.source_state as { state: number; validity: string; purpose: string; current: string | null } | null
  const [reason, setReason] = useState('')
  const [purpose, setPurpose] = useState<'long_term' | 'transient'>('long_term')
  const requestId = useRef(crypto.randomUUID())
  const documentId = typeof source.document_id === 'string' ? source.document_id : ''
  const dispatch = async (name: string) => {
    if (!state) return
    await onAction(name, { document_id: documentId, expected_state: state.state, reason: name === 'withdraw' ? undefined : reason.trim(), purpose, request_id: requestId.current })
  }
  useEffect(() => { requestId.current = crypto.randomUUID() }, [state?.state])
  return <section className="border-t border-default pt-3 space-y-2">
    <div className="text-xs break-all text-content-tertiary">资料 {documentId || '未登记'} · 版本 {String(source.revision_id || '未登记')}{state && ` · ${purposeLabels[state.purpose] || state.purpose} · ${validityLabels[state.validity] || state.validity}`} · {source.current_searchable ? '当前可检索' : '当前不可检索'}</div>
    <div className="flex gap-2 flex-wrap">
      {typeof source.publish_id === 'string' && <button className={btn} onClick={onReference}>查看出处</button>}
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

function locationLabel(location: Record<string, unknown>) {
  const labels: Record<string, string> = { page: '物理页', physical_page: '物理页', sheet: '工作表', cell: '单元格', range: '区域', paragraph: '段落', section: '章节', table: '表格', row: '行', column: '列', line: '行' }
  return Object.entries(location).map(([key, value]) => `${labels[key] || key} ${typeof value === 'object' ? JSON.stringify(value) : String(value)}`).join(' · ')
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
      <span title="按解析版本的出处单位编号，非文档页码；页号、段落或单元格见原文位置。">?</span>
    </div>
    {error && <div role="alert" className="text-danger">{error}</div>}
    {loading ? <div role="status">读取中…</div> : reference && <>
      <div className="text-sm">{reference.current_status === 'current' ? '当前有效' : reference.current_status === 'withdrawn' ? '已撤回' : reference.current_status === 'corrected' ? '已更正' : '已替代或存在未发布变更'} · {locationLabel(reference.location)}</div>
      <details><summary className="cursor-pointer text-sm">出处目录</summary><div className="flex flex-wrap gap-2 py-2">{reference.units.slice(Math.floor(ordinal / 50) * 50, Math.floor(ordinal / 50) * 50 + 50).map(unit => <button key={unit.ordinal} className={btn} onClick={() => setOrdinal(unit.ordinal)} aria-current={unit.ordinal === ordinal}>{unit.ordinal + 1}. {locationLabel(unit.location)}</button>)}</div></details>
      <Markdown content={reference.text} />
    </>}
  </section>
}
