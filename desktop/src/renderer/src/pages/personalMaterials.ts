import type { PersonalTask } from '../api/client'

export type SourceRef = Record<string, unknown>
export type MaterialAttachment = PersonalTask['attachments'][number] & {
  source_document_id?: string
  source_revision_id?: string
}
export type MaterialTask = PersonalTask & { origin?: { parent_task_id?: string } }
export interface Material {
  key: string
  documentId?: string
  recordOnly: boolean
  title: string
  tasks: PersonalTask[]
  originals: { task: PersonalTask; attachment: MaterialAttachment }[]
  results: { task: PersonalTask; ref: SourceRef }[]
  sources: { task: PersonalTask; source: SourceRef }[]
}

export const taskStatuses: Record<string, string> = { queued: '已接收', running: '处理中', waiting_input: '等待补充', succeeded: '已完成', failed: '失败', cancelled: '已取消', needs_reconcile: '待核对' }
export const taskStages: Record<string, string> = { receive: '接收', download: '保存原件', decide: '判断用途', parse: '解析', publish: '发布', pending_index: '已保存，待索引', done: '完成' }
export function isTransportFilename(value: string): boolean {
  const name = value.replace(/\\/g, '/').split('/').pop() || ''
  return /^file_v\d+_/i.test(name) || /^(?:[a-f0-9]{32}|[a-f0-9]{64})(?:\.[^.]+)?$/i.test(name)
}
export function readableFilename(value: unknown, fallback = '来源原件'): string {
  return typeof value === 'string' && value.trim() && !isTransportFilename(value) ? value : fallback
}
export function personalTaskTitle(task: PersonalTask): string {
  const title = task.result_refs.find(ref => typeof ref.title === 'string' && ref.title.trim() && !isTransportFilename(ref.title))?.title
  if (typeof title === 'string') return title
  const filenames = task.attachments.map(item => item.filename).filter(Boolean)
  if (!filenames.length && task.payload?.filename) filenames.push(task.payload.filename)
  return filenames.map(name => readableFilename(name, '资料处理任务')).join('、') || '知识整理任务'
}
export function personalOutcomeLabel(task: PersonalTask): string {
  if (task.outcome !== 'published') return ({ transient: '临时使用', duplicate: '重复资料' } as Record<string, string>)[task.outcome || ''] || task.outcome || ''
  const refs = task.result_refs.filter(ref => typeof ref.publish_id === 'string')
  const verified = refs.filter(ref => ref.current_searchable === true).length
  return !verified ? '已发布（当前不可检索）' : verified === refs.length ? '当前可检索' : '部分可检索'
}
export const canResubmit = (task: PersonalTask) => task.status === 'failed' && task.error?.code === 'config_unavailable' && task.attachments.length > 0 && !task.result_refs.length && !task.payload?.source_action
export function taskActions(task: PersonalTask): string[] {
  const actions = canResubmit(task) ? ['resubmit'] : task.status === 'failed' && task.error?.code !== 'config_unavailable' && task.error?.retryable ? ['retry'] : []
  if (task.status === 'waiting_input') actions.push('update_input')
  if (['queued', 'running', 'waiting_input'].includes(task.status)) actions.push('cancel')
  return actions
}
export const personalKnowledgeFingerprint = (tasks: PersonalTask[]) => JSON.stringify(tasks.flatMap(task => task.result_refs.filter(ref => typeof ref.publish_id === 'string').map(ref => JSON.stringify([
  task.task_id, task.status, ref.page_id, ref.page_version, ref.publish_id, ref.current_searchable === true, ref.title, ref.category, ref.path,
]))).sort())

const sourcesOf = (ref: SourceRef): SourceRef[] => Array.isArray(ref.sources) && ref.sources.length ? ref.sources as SourceRef[] : ref.document_id ? [ref] : []
const attachmentKey = (taskId: string, attachmentId: string) => JSON.stringify(['attachment', taskId, attachmentId])
const documentKey = (id: string) => JSON.stringify(['document', id])
const timeOf = (task: PersonalTask) => Date.parse(task.updated_at) || 0

/** 只使用受信来源ID，或父子任务中复制的同一附件。哈希仅核对复制，不推断来源。 */
export function buildMaterials(input: PersonalTask[]): Material[] {
  const tasks = [...input].sort((a, b) => timeOf(b) - timeOf(a) || a.task_id.localeCompare(b.task_id)) as MaterialTask[]
  const byId = new Map(tasks.map(task => [task.task_id, task]))
  const roots = new Map<string, string>()
  const root = (key: string): string => {
    const parent = roots.get(key)
    return parent && parent !== key ? root(parent) : key
  }
  const join = (a: string, b: string) => {
    a = root(a); b = root(b)
    if (a === b || (a.startsWith('["document"') && b.startsWith('["document"'))) return
    if (a.startsWith('["document"')) roots.set(b, a)
    else roots.set(a, b)
  }
  // 先建立附件到已登记资料的关联，再处理复制链，避免跨source合并。
  for (const task of tasks) for (const attachment of task.attachments as MaterialAttachment[]) {
    if (attachment.source_document_id) join(attachmentKey(task.task_id, attachment.attachment_id), documentKey(attachment.source_document_id))
  }
  for (const task of tasks) {
    const parent = byId.get(task.origin?.parent_task_id || '')
    if (!parent) continue
    for (const attachment of task.attachments) {
      const copied = parent.attachments.find(item => item.attachment_id === attachment.attachment_id && item.sha256 === attachment.sha256)
      if (copied) join(attachmentKey(task.task_id, attachment.attachment_id), attachmentKey(parent.task_id, copied.attachment_id))
    }
  }
  const groups = new Map<string, Material>()
  const ensure = (key: string, documentId?: string, recordOnly = false): Material => {
    key = root(key)
    if (!groups.has(key)) groups.set(key, { key, documentId: documentId || (key.startsWith('["document"') ? JSON.parse(key)[1] : undefined), recordOnly,
      title: '', tasks: [], originals: [], results: [], sources: [] })
    return groups.get(key)!
  }
  const addTask = (group: Material, task: PersonalTask) => { if (!group.tasks.some(item => item.task_id === task.task_id)) group.tasks.push(task) }
  for (const task of tasks) {
    for (const attachment of task.attachments as MaterialAttachment[]) {
      const group = ensure(attachmentKey(task.task_id, attachment.attachment_id))
      group.originals.push({ task, attachment }); addTask(group, task)
    }
    for (const ref of task.result_refs) for (const source of sourcesOf(ref)) {
      if (typeof source.document_id !== 'string') continue
      const group = ensure(documentKey(source.document_id), source.document_id)
      addTask(group, task)
      group.sources.push({ task, source })
      if (!group.results.some(item => item.ref.publish_id === ref.publish_id && item.ref.page_id === ref.page_id)) group.results.push({ task, ref })
    }
    if (!task.attachments.length) {
      const group = ensure(JSON.stringify(['record', task.task_id]), undefined, true)
      addTask(group, task)
      group.results = task.result_refs.map(ref => ({ task, ref }))
    }
  }
  for (const group of groups.values()) {
    // 同一资料不同任务中的原件只显示一份；不同内容修订仍留在历史下载中。
    const original = group.originals[0]
    const filenames = [...group.originals.map(item => item.attachment.filename), ...group.sources.map(item => item.source.filename)]
    const filename = filenames.find(value => typeof value === 'string' && value.trim() && !isTransportFilename(value))
    const page = group.results.find(item => item.ref.current_searchable === true && typeof item.ref.title === 'string') || group.results.find(item => typeof item.ref.title === 'string')
    group.title = group.recordOnly ? personalTaskTitle(group.tasks[0]) : typeof filename === 'string' ? filename :
      page ? readableFilename(page.ref.title, '资料处理任务') : original ? readableFilename(original.attachment.filename, '资料处理任务') : '资料处理任务'
  }
  return [...groups.values()].sort((a, b) => timeOf(b.tasks[0]) - timeOf(a.tasks[0]) || a.key.localeCompare(b.key))
}

/** 选当前状态修订最高的来源回执；授权动作始终回到该回执所属任务。 */
export function materialSource(material: Material): Material['sources'][number] | undefined {
  return [...material.sources].sort((a, b) => Number((b.source.source_state as SourceRef | undefined)?.state ?? -1) - Number((a.source.source_state as SourceRef | undefined)?.state ?? -1))[0]
}
export function materialResults(material: Material): Material['results'] {
  const pages = new Map<string, Material['results'][number]>()
  for (const result of material.results) {
    if (typeof result.ref.publish_id !== 'string' || typeof result.ref.page_id !== 'string') continue
    const key = String(result.ref.page_id || result.ref.publish_id)
    const previous = pages.get(key)
    if (!previous || (result.ref.current_searchable === true && previous.ref.current_searchable !== true)) pages.set(key, result)
  }
  return [...pages.values()]
}
export function materialKnowledgeLabel(material: Material): string {
  const results = materialResults(material)
  const valid = results.filter(item => item.ref.current_searchable === true).length
  return valid ? `${valid} 篇当前可检索` : results.length ? '当前不可检索' : '尚无知识结果'
}

export function materialReorganizeSource(material: Material): Material['sources'][number] | undefined {
  return material.sources.find(({ task, source }) => source.document_id === material.documentId &&
    material.originals.some(original => original.task.task_id === task.task_id && original.attachment.source_document_id === material.documentId))
}
