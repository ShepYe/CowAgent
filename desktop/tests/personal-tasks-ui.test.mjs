import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { fileURLToPath } from 'node:url'

// 仅隔离网络、弹框与正文渲染，状态标签和来源控件使用真实组件。
const result = await build({
  entryPoints: [fileURLToPath(new URL('../src/renderer/src/pages/PersonalTasksPanel.tsx', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs', external: ['react', 'react/jsx-runtime'],
  plugins: [{ name: 'offline-ui', setup(builder) {
    builder.onResolve({ filter: /(?:api\/client|components\/(?:Markdown|Tooltip)|store\/confirmStore|i18n)$/ }, ({ path }) => ({ path, namespace: 'offline' }))
    builder.onLoad({ filter: /.*/, namespace: 'offline' }, ({ path }) => ({ contents: path.endsWith('api/client')
      ? 'export default {personalOriginal: (task, attachment) => `/original/${task}/${attachment}`, personalTasks: () => globalThis.__personalTasksForUi()};'
      : path.endsWith('Tooltip') ? 'export default function Tooltip({children}) { return children }'
      : 'export default function Stub() { return null }; export const askConfirm = async () => false; export const t = key => key;' }))
  } }],
})
const module = { exports: {} }
new Function('require', 'module', 'exports', result.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports)
const { default: PersonalTasksPanel, personalOutcomeLabel, personalKnowledgeFingerprint, personalTaskTitle, SourceControls, MaterialDetail, MaterialsLayout, TaskHistory, SourceManagementForm, SourceManager, PurposeSelect, KnowledgeResult } = module.exports
const published = (...refs) => ({ outcome: 'published', result_refs: refs })

const modelBuild = await build({ entryPoints: [fileURLToPath(new URL('../src/renderer/src/pages/personalMaterials.ts', import.meta.url))], bundle: true, write: false, platform: 'node', format: 'cjs' })
const model = { exports: {} }
new Function('module', 'exports', modelBuild.outputFiles[0].text)(model, model.exports)
const { buildMaterials, materialResults, materialKnowledgeLabel, taskActions } = model.exports
const task = (id, changes = {}) => ({ task_id: id, status: 'succeeded', stage: 'done', attempt: 1, updated_at: '2026-11-11T10:00:00Z', attachments: [], result_refs: [], ...changes })
const attachment = (id, documentId, changes = {}) => ({ attachment_id: id, filename: '同名资料.pdf', sha256: 'same-bytes', ...(documentId ? { source_document_id: documentId, source_revision_id: 'rev-' + documentId } : {}), ...changes })
const source = (documentId, changes = {}) => ({ document_id: documentId, revision_id: 'rev-' + documentId, filename: '原件.pdf', source_state: { state: 3, purpose: 'long_term', validity: 'active', current: 'rev-' + documentId }, ...changes })
const page = (id, sources, changes = {}) => ({ page_id: id, publish_id: 'publish-' + id, path: 'knowledge/wiki/' + id + '.md', title: '整理主题', category: '技术', current_searchable: true, ...sources[0], sources, ...changes })

// 直接驱动组件返回的事件处理器；不启动浏览器或模型，也不引入新测试依赖。
function hooks(Component, initialProps) {
  const cells = [], effects = []
  let cursor = 0, props = initialProps
  const cell = (initial) => { const index = cursor++; if (!(index in cells)) cells[index] = typeof initial === 'function' ? initial() : initial; return index }
  const dispatcher = {
    useState(initial) { const index = cell(initial); return [cells[index], value => { cells[index] = typeof value === 'function' ? value(cells[index]) : value }] },
    useRef(initial) { const index = cell(() => ({ current: initial })); return cells[index] },
    useMemo(fn) { cursor++; return fn() }, useCallback(fn) { cursor++; return fn },
    useEffect(fn, deps) { const index = cursor++; const previous = cells[index]; if (!previous || deps.some((value, i) => value !== previous.deps[i])) { cells[index] = { deps }; effects.push(fn) } },
  }
  return {
    render(next = props) { props = next; cursor = 0; const internal = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentDispatcher; const previous = internal.current; internal.current = dispatcher; try { return Component(props) } finally { internal.current = previous } },
    effects() { return effects.splice(0).map(fn => fn()).filter(Boolean) },
  }
}
function nodes(element, predicate) {
  const found = []
  const walk = item => { if (!React.isValidElement(item)) return; if (predicate(item)) found.push(item); React.Children.forEach(item.props.children, walk) }
  walk(element)
  return found
}
const textOf = element => React.Children.toArray(element?.props?.children).map(child => typeof child === 'string' || typeof child === 'number' ? String(child) : React.isValidElement(child) ? textOf(child) : '').join('')

test('资料轮询等待变更核验，失败后重试，成功后相同快照不重复刷新', async () => {
  globalThis.__personalTasksForUi = async () => ({ status: 'success', tasks: [] })
  let resolve, calls = 0
  const pending = new Promise(done => { resolve = done })
  const h = hooks(PersonalTasksPanel, { onKnowledgeChanged: () => ++calls === 1 ? pending : true })
  const refresh = () => nodes(h.render(), n => n.props['aria-label'] === '刷新资料')[0].props.onClick()
  const settle = () => new Promise(done => setImmediate(done))
  refresh(); await settle()
  refresh(); await settle()
  assert.equal(calls, 1, '上次核验待返回时不能用轮询反复使其失效')
  resolve(false); await settle()
  refresh(); await settle()
  assert.equal(calls, 2, '核验失败没有消耗同一指纹的重试机会')
  refresh(); await settle()
  assert.equal(calls, 2, '核验成功才确认指纹')
})

test('资料按可信document归并，多附件、同名和同哈希不同source不会误合并', () => {
  const data = buildMaterials([task('multi', { attachments: [attachment('a', 'A'), attachment('b', 'B')], result_refs: [page('merged', [source('A'), source('B')])] }),
    task('unregistered-1', { attachments: [attachment('a')] }), task('unregistered-2', { attachments: [attachment('a')] })])
  assert.equal(data.length, 4)
  assert.equal(data.filter(item => item.documentId).length, 2)
  assert.equal(data.find(item => item.documentId === 'A').originals[0].attachment.attachment_id, 'a')
  assert.equal(data.find(item => item.documentId === 'B').originals[0].attachment.attachment_id, 'b')
  assert.notEqual(data.find(item => item.tasks[0].task_id === 'unregistered-1').key, data.find(item => item.tasks[0].task_id === 'unregistered-2').key)
})

test('只有确切父子附件复制链可归并，未证实关联的旧失败保留独立', () => {
  const data = buildMaterials([task('root', { status: 'failed', attachments: [attachment('a')] }),
    task('child', { origin: { parent_task_id: 'root' }, attachments: [attachment('a', 'A')], result_refs: [page('one', [source('A')])] }),
    task('unrelated-old-failure', { status: 'failed', attachments: [attachment('a')] }),
    task('other-child', { origin: { parent_task_id: 'root' }, attachments: [attachment('different')] })])
  assert.equal(data.length, 3)
  assert.deepEqual(data.find(item => item.documentId === 'A').tasks.map(item => item.task_id).sort(), ['child', 'root'])
  assert.equal(data.find(item => item.tasks[0].task_id === 'unrelated-old-failure').documentId, undefined)
  const conflicting = buildMaterials([task('parent', { attachments: [attachment('a', 'A')] }), task('child', { origin: { parent_task_id: 'parent' }, attachments: [attachment('a', 'B')] })])
  assert.equal(conflicting.length, 2)
})

test('重复处理折叠成资料历史，无附件任务仍有独立记录且失败不抹掉旧知识', () => {
  const original = task('old', { attachments: [attachment('a', 'A')], result_refs: [page('one', [source('A')]), page('two', [source('A')])] })
  const latest = task('latest', { updated_at: '2026-11-11T11:00:00Z', status: 'failed', attachments: [attachment('a', 'A')], error: { code: 'index_failed', retryable: true } })
  const data = buildMaterials([original, latest, task('synthesis', { result_refs: [page('three', [source('A')])] })])
  const material = data.find(item => item.documentId === 'A')
  assert.equal(material.tasks[0].task_id, 'latest')
  assert.equal(materialKnowledgeLabel(material), '3 篇当前可检索')
  assert.equal(data.filter(item => item.recordOnly).length, 1)
  const html = renderToStaticMarkup(React.createElement(MaterialDetail, { material, busy: false, onAction: async () => true, onReference() {}, onOpenKnowledge() {} }))
  assert.match(html, /旧知识仍有效/)
  assert.equal((html.match(/aria-label="原件与视觉"/g) || []).length, 1)
  assert.equal((html.match(/>来源管理</g) || []).length, 1)
  assert.equal((html.match(/>重新整理</g) || []).length, 1)
  assert.equal(materialResults(material).length, 3)
})

test('资料列表可进入详情，窄屏提供返回且不暴露内部身份', () => {
  const material = buildMaterials([task('t', { attachments: [attachment('a', 'A')] })])[0]
  let selected = null
  const harness = hooks(MaterialsLayout, { materials: [material], selected: null, onSelect: key => { selected = key }, onBack: () => { selected = null } })
  const list = harness.render()
  nodes(list, node => node.type === 'button' && 'aria-current' in node.props)[0].props.onClick()
  assert.equal(selected, material.key)
  const detail = harness.render({ materials: [material], active: material, selected, onSelect() {}, onBack: () => { selected = null } })
  const listNode = nodes(detail, node => node.props['aria-label'] === '资料列表')[0]
  assert.match(listNode.props.className, /hidden lg:block/)
  assert.doesNotMatch(listNode.props.className, /md:/)
  assert.match(nodes(detail, node => node.props['aria-label'] === '资料详情')[0].props.className, /flex/)
  assert.ok(nodes(detail, node => node.props.className?.includes('lg:hidden')).length)
  const previous = globalThis.requestAnimationFrame
  globalThis.requestAnimationFrame = fn => fn()
  try { nodes(detail, node => node.type === 'button' && textOf(node).includes('返回资料列表'))[0].props.onClick() } finally { globalThis.requestAnimationFrame = previous }
  assert.equal(selected, null)
})

test('知识结果窄容器中标题保留空间，操作允许换行而非强制挤压正文', () => {
  const tree = KnowledgeResult({ source: page('one', [source('A')]), onReference() {}, onOpenKnowledge() {} })
  assert.match(tree.props.className, /flex-wrap/)
  assert.match(React.Children.toArray(tree.props.children)[0].props.className, /basis-48/)
  const actions = React.Children.toArray(tree.props.children)[1]
  assert.match(actions.props.className, /flex-wrap/)
  assert.doesNotMatch(actions.props.className, /shrink-0/)
})

test('退役原文投影只进入原处理历史，当前区按稳定Wiki身份去重并保留暂停页', () => {
  const legacy = { document_id: 'A', revision_id: 'rev-A', publish_id: 'legacy-pub', path: 'knowledge/managed/opaque.md', title: '不是判断依据', current_searchable: false }
  const old = task('legacy', { attachments: [attachment('a', 'A')], result_refs: [legacy] })
  const wikiRefs = Array.from({ length: 4 }, (_, index) => page('wiki-' + index, [source('A')], { current_searchable: index !== 3 }))
  const newer = task('wiki', { updated_at: '2026-11-11T12:00:00Z', attachments: [attachment('a', 'A')], result_refs: wikiRefs })
  const input = [old, newer]
  const before = JSON.stringify(input)
  const material = buildMaterials(input)[0]
  assert.equal(materialResults(material).length, 4)
  assert.ok(materialResults(material).every(item => item.ref.page_id))
  assert.equal(materialResults(material).filter(item => item.ref.current_searchable === false).length, 1)
  const detail = MaterialDetail // 通过hooks检查传给历史组件的原回执与回查动作。
  const calls = []
  const harness = hooks(detail, { material, busy: false, onAction: async () => true, onReference: (...args) => calls.push(args) })
  const history = nodes(harness.render(), node => node.type === TaskHistory && node.props.task.task_id === 'legacy')[0]
  assert.equal(history.props.historyResults[0].publish_id, 'legacy-pub')
  const historyHarness = hooks(TaskHistory, history.props)
  const historyTree = historyHarness.render()
  nodes(historyTree, node => textOf(node) === '查看历史出处' && node.props.onClick)[0].props.onClick()
  assert.equal(calls[0][0].task_id, 'legacy')
  assert.equal(calls[0][1].publish_id, 'legacy-pub')
  assert.equal(JSON.stringify(input), before)
})

test('无附件记录置于底部默认折叠组，资料主体仅计原件，展开记录仍可进入详情', () => {
  const materials = buildMaterials([task('pdf', { attachments: [attachment('a', 'A')] }), task('chat-1'), task('chat-2')])
  let selected
  const harness = hooks(MaterialsLayout, { materials, selected: null, onSelect: key => { selected = key }, onBack() {} })
  const tree = harness.render()
  const group = nodes(tree, node => node.type === 'details' && node.props['aria-label'] === '其他处理记录')[0]
  assert.equal(group.props.open, undefined)
  assert.match(textOf(nodes(group, node => node.type === 'summary')[0]), /其他处理记录（2）/)
  nodes(group, node => node.type === 'button')[0].props.onClick()
  assert.ok(materials.find(item => item.key === selected).recordOnly)
  const active = materials.find(item => item.key === selected)
  const next = harness.render({ materials, selected, active, onSelect() {}, onBack() {} })
  assert.equal(nodes(next, node => node.type === 'details' && node.props['aria-label'] === '其他处理记录')[0].props.open, true)
  const html = renderToStaticMarkup(tree)
  assert.match(html.replace(/<!--.*?-->/g, ''), /资料 · 1/)
})

test('来源管理先选操作，仅展示所需字段和一个提交动作', () => {
  const calls = []
  const props = { source: source('A'), busy: false, reason: '已核对', purpose: 'transient', onReason() {}, onPurpose() {}, onSubmit: name => calls.push(name), onClose() {} }
  const harness = hooks(SourceManagementForm, props)
  let tree = harness.render()
  assert.equal(nodes(tree, node => node.props['aria-label'] === '更正或用途修订依据').length, 0)
  for (const [label, action, submit] of [['修改用途', 'change_purpose', '保存用途'], ['标记内容有误', 'correct', '标记内容有误'], ['撤回资料', 'withdraw', '撤回资料']]) {
    nodes(tree, node => node.type === 'button' && textOf(node) === label)[0].props.onClick()
    tree = harness.render()
    assert.equal(nodes(tree, node => node.type === PurposeSelect).length, action === 'change_purpose' ? 1 : 0)
    assert.equal(nodes(tree, node => node.props['aria-label'] === '更正或用途修订依据').length, action === 'withdraw' ? 0 : 1)
    const submits = nodes(tree, node => typeof node.type === 'function' && node.props.onClick && textOf(node) === submit)
    assert.equal(submits.length, 1)
    submits[0].props.onClick()
    assert.equal(calls.at(-1), action)
  }
  const restored = hooks(SourceManagementForm, { ...props, source: source('A', { source_state: { state: 4, purpose: 'long_term', validity: 'corrected', current: null } }) }).render()
  assert.equal(nodes(restored, node => node.type === 'button' && textOf(node) === '标记内容有误').length, 0)
  assert.equal(nodes(restored, node => node.type === 'button' && textOf(node) === '重新长期发布').length, 1)
})

test('历史动作按真实任务和attempt执行，补充输入受空值和busy守卫', () => {
  const waiting = task('waiting', { status: 'waiting_input', attempt: 4, question: '用于哪种场景？' })
  const calls = []
  const props = { task: waiting, busy: false, latest: true, onAction: (...args) => calls.push(args) }
  const harness = hooks(TaskHistory, props)
  let tree = harness.render()
  const submit = value => nodes(value, node => textOf(node) === '补充用途' && typeof node.props.onClick === 'function')[0]
  assert.equal(submit(tree).props.disabled, true)
  nodes(tree, node => node.props['aria-label'] === '补充用途')[0].props.onChange({ target: { value: '仅临时使用' } })
  tree = harness.render()
  assert.equal(submit(tree).props.disabled, false)
  submit(tree).props.onClick()
  assert.equal(calls[0][0].attempt, 4)
  assert.deepEqual(calls[0].slice(1), ['update_input', undefined, '仅临时使用'])
  assert.equal(submit(harness.render({ ...props, busy: true })).props.disabled, true)
  assert.deepEqual(taskActions(task('queued', { status: 'queued' })), ['cancel'])
  assert.deepEqual(taskActions(task('done')), [])
  assert.deepEqual(taskActions(task('failed', { status: 'failed', error: { code: 'index_failed', retryable: true } })), ['retry'])
  assert.deepEqual(taskActions(task('config', { status: 'failed', attachments: [attachment('a')], error: { code: 'config_unavailable' } })), ['resubmit'])
})

test('来源管理集中动作且用途选项可Tab/Enter，点击传递当前expected_state', async () => {
  const calls = []
  const manager = hooks(SourceManager, { source: source('B'), busy: false, onAction: async (...args) => { calls.push(args); return true } })
  let tree = manager.render()
  nodes(tree, node => node.type === 'button' && textOf(node).includes('来源管理'))[0].props.onClick()
  tree = manager.render()
  const form = nodes(tree, node => node.type === SourceManagementForm)[0]
  form.props.onReason('依据更正')
  form.props.onPurpose('transient')
  tree = manager.render()
  nodes(tree, node => node.type === SourceManagementForm)[0].props.onSubmit('change_purpose')
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(calls[0][0], 'change_purpose')
  assert.equal(calls[0][1].document_id, 'B')
  assert.equal(calls[0][1].expected_state, 3)
  assert.equal(calls[0][1].purpose, 'transient')
  assert.equal(calls[0][1].reason, '依据更正')
  const selector = hooks(PurposeSelect, { value: 'long_term', disabled: false, onChange: value => calls.push(value) })
  let menu = selector.render()
  nodes(menu, node => node.type === 'button')[0].props.onClick()
  menu = selector.render()
  const option = nodes(menu, node => node.props.role === 'option' && textOf(node) === '临时使用')[0]
  assert.equal(option.type, 'button')
  option.props.onClick() // 原生button的Enter/Space路径与点击一致。
  assert.equal(calls.at(-1), 'transient')
  assert.equal(nodes(selector.render(), node => node.props.role === 'listbox').length, 0)
  const formHarness = hooks(SourceManagementForm, { source: source('B'), busy: false, reason: '', purpose: 'long_term', onReason() {}, onPurpose() {}, onSubmit() {}, onClose() {} })
  let formTree = formHarness.render()
  assert.ok(nodes(formTree, node => typeof node.props.label === 'string' && node.props.label.includes('暂停所有引用该资料的知识页及其派生内容')).length)
  assert.equal(nodes(formTree, node => node.type === PurposeSelect).length, 0)
  nodes(formTree, node => node.type === 'button' && textOf(node) === '修改用途')[0].props.onClick()
  formTree = formHarness.render()
  assert.equal(nodes(formTree, node => node.type === PurposeSelect).length, 1)
  assert.ok(nodes(formTree, node => textOf(node) === '保存用途' && node.props.onClick).length)

})

test('来源管理Esc关闭并将焦点返回触发按钮，Tab在弹层内循环', () => {
  const previousWindow = globalThis.window, previousDocument = globalThis.document
  const handlers = new Map()
  let focused = '', first, last
  first = { focus() { focused = 'first' } }; last = { focus() { focused = 'last' } }
  globalThis.window = { addEventListener: (key, fn) => handlers.set(key, fn), removeEventListener() {} }
  globalThis.document = { activeElement: last }
  const harness = hooks(SourceManager, { source: source('A'), busy: false, onAction: async () => true })
  try {
    let tree = harness.render()
    const trigger = nodes(tree, node => node.type === 'button')[0]
    trigger.ref.current = { focus() { focused = 'trigger' } }
    trigger.props.onClick(); tree = harness.render()
    const body = nodes(tree, node => node.props.role === 'dialog')[0]
    body.ref.current = { querySelector: selector => {
      assert.equal(selector, '[aria-label="选择来源操作"] button:not(:disabled)')
      return first
    }, parentElement: { parentElement: { querySelector: () => last, querySelectorAll: () => [first, last] } } }
    harness.effects()
    assert.equal(focused, 'first')
    let prevented = false
    handlers.get('keydown')({ key: 'Tab', preventDefault() { prevented = true } })
    assert.equal(prevented, true); assert.equal(focused, 'first')
    handlers.get('keydown')({ key: 'Escape', preventDefault() {}, stopPropagation() {} })
    assert.equal(focused, 'trigger')
    assert.equal(nodes(harness.render(), node => node.props.title === '来源管理')[0].props.open, false)
  } finally { globalThis.window = previousWindow; globalThis.document = previousDocument }
})

test('来源管理无可用操作按钮时初始焦点回退到关闭按钮', () => {
  const previousWindow = globalThis.window
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  let focused = false
  try {
    const harness = hooks(SourceManager, { source: source('A'), busy: false, onAction: async () => true })
    nodes(harness.render(), node => node.type === 'button')[0].props.onClick()
    const body = nodes(harness.render(), node => node.props.role === 'dialog')[0]
    body.ref.current = { querySelector: () => null, parentElement: { parentElement: { querySelector: selector => {
      assert.equal(selector, 'button:not(:disabled)')
      return { focus() { focused = true } }
    } } } }
    harness.effects()
    assert.equal(focused, true)
  } finally { globalThis.window = previousWindow }
})

test('无附件的来源动作和知识整理任务不会将内部编号作为标题', () => {
  const task = { task_id: 'internal-task-id', attachments: [], result_refs: [], payload: { source_action: {} } }
  assert.equal(personalTaskTitle(task), '知识整理任务')
  assert.equal(personalTaskTitle({ ...task, result_refs: [{ title: '' }, { title: '跨资料整理主题' }] }), '跨资料整理主题')
  assert.equal(personalTaskTitle({ ...task, attachments: [{ filename: '原始资料.pdf' }], result_refs: [{ title: '主题' }] }), '主题')
  assert.equal(personalTaskTitle({ ...task, attachments: [{ filename: '原始资料.pdf' }] }), '原始资料.pdf')
  for (const filename of ['file_v3_remote_key.pdf', 'a'.repeat(32), `${'b'.repeat(64)}.pdf`]) {
    const input = { ...task, attachments: [{ filename }], result_refs: [{ title: filename.replace(/\.pdf$/, '') }] }
    assert.equal(personalTaskTitle(input), '资料处理任务')
    assert.equal(input.attachments[0].filename, filename)
  }
  assert.equal(personalTaskTitle({ ...task, attachments: [{ filename: 'file_version_notes.pdf' }] }), 'file_version_notes.pdf')
})

test('知识指纹只随发布结果变化，忽略轮询时间及任务与回执排序', () => {
  const parent = { task_id: 'parent', status: 'succeeded', updated_at: 'before', result_refs: [{ publish_id: 'old', current_searchable: true }] }
  const child = { task_id: 'child', status: 'running', result_refs: [] }
  const initial = personalKnowledgeFingerprint([parent, child])
  assert.equal(personalKnowledgeFingerprint([{ ...child, updated_at: 'later', stage: 'parse' }, { ...parent, updated_at: 'later' }]), initial)
  const complete = { ...child, status: 'succeeded', result_refs: Array.from({ length: 4 }, (_, i) => ({ page_id: `page-${i}`, publish_id: `publish-${i}`, current_searchable: true })) }
  const finished = personalKnowledgeFingerprint([parent, complete])
  assert.notEqual(finished, initial)
  assert.equal(personalKnowledgeFingerprint([{ ...complete, result_refs: [...complete.result_refs].reverse() }, parent]), finished)
  assert.notEqual(personalKnowledgeFingerprint([{ ...parent, result_refs: [{ publish_id: 'old', current_searchable: false }] }, complete]), finished)
  assert.notEqual(personalKnowledgeFingerprint([parent, { ...complete, result_refs: complete.result_refs.slice(1) }]), finished)
})

test('发布回执只有当前核验通过才标可检索', () => {
  assert.equal(personalOutcomeLabel(published()), '已发布（当前不可检索）')
  assert.equal(personalOutcomeLabel(published({ publish_id: 'page', searchable: true })), '已发布（当前不可检索）')
  assert.equal(personalOutcomeLabel(published({ publish_id: 'page', current_searchable: 'true' })), '已发布（当前不可检索）')
  assert.equal(personalOutcomeLabel(published({ publish_id: 'page', current_searchable: true })), '当前可检索')
  assert.equal(personalOutcomeLabel(published({ publish_id: 'page', current_searchable: true }, { publish_id: 'old', current_searchable: false })), '部分可检索')
})

test('来源控件独立展示视觉缺口且保留未核验页禁用状态', () => {
  const source = { page_id: 'internal-page', path: 'knowledge/wiki/技术/internal.md', filename: '真实原件.pdf', title: '整理主题', category: '技术', publish_id: 'publication', searchable: true, current_searchable: false,
    visual_coverage: [{ physical_page: 7, unprocessed_reason: 'vision_unavailable' }] }
  const render = (current_searchable) => renderToStaticMarkup(React.createElement(SourceControls, {
    source: { ...source, current_searchable }, busy: false, onAction: async () => {}, onReference() {}, onOpenKnowledge() {},
  }))
  const unverified = render('true')
  assert.match(unverified, /当前不可检索/)
  assert.match(unverified, /disabled=""[^>]*>查看知识页/)
  assert.match(unverified, /物理页 7/)
  assert.doesNotMatch(unverified, /internal-page|internal\.md/)
  const verified = render(true)
  assert.match(verified, /当前可检索/)
  assert.match(verified, /视觉：1页待处理/)
  assert.doesNotMatch(verified, /disabled=""[^>]*>查看知识页/)
})

test('传输原件名仅悬停显示，视觉覆盖按状态紧凑计数且独立于可检索', () => {
  const filename = 'file_v3_transport_original.pdf'
  const source = { filename, title: filename, current_searchable: true, visual_coverage: Array.from({ length: 29 }, (_, i) => ({
    physical_page: i + 1, status: i < 20 ? 'processed' : 'partial', unprocessed_reason: i < 20 ? null : '图表仍有不清晰区域',
  })) }
  const render = (value) => renderToStaticMarkup(React.createElement(SourceControls, {
    source: value, busy: false, onAction: async () => {}, onReference() {},
  }))
  const html = render(source)
  const visible = html.replace(/<[^>]*>/g, '')
  assert.match(html, /title="file_v3_transport_original.pdf">来源原件/)
  assert.doesNotMatch(visible, /file_v3_|物理页|图表仍有不清晰区域/)
  assert.match(visible, /视觉：20页已处理 · 9页待核验/)
  assert.match(visible, /当前可检索/)
  assert.match(html, /物理页 29：待核验（图表仍有不清晰区域）/)
  const pending = render({ ...source, visual_coverage: [...source.visual_coverage, { physical_page: 30, status: 'pending', unprocessed_reason: 'vision_unavailable' }] })
  assert.match(pending, /视觉：20页已处理 · 9页待核验 · 1页待处理/)
  assert.match(pending, /物理页 30：待处理（视觉模型不可用）/)
  assert.match(render({ filename: '射频设计说明.pdf' }), />射频设计说明.pdf</)
  for (const name of [`${'c'.repeat(32)}.pdf`, 'd'.repeat(64)]) assert.match(render({ filename: name }), />来源原件</)
  assert.equal(source.filename, filename)
})

test('多来源页只展示一次页面入口，每份原件独立呈现来源状态与动作', () => {
  const first = { document_id: 'doc-a', revision_id: 'rev-a', filename: '原件A.docx', source_status: 'current',
    source_state: { state: 1, validity: 'active', purpose: 'long_term', current: 'rev-a' } }
  const second = { document_id: 'doc-b', revision_id: 'rev-b', filename: '原件B.xlsx', source_status: 'current',
    source_state: { state: 3, validity: 'active', purpose: 'long_term', current: 'rev-b' }, visual_warnings: [
      { kind: 'image', part: 'word/media/image.png', status: 'pending', reason: 'visual_not_supported' },
      { kind: 'chart', part: 'xl/charts/chart1.xml', status: 'pending', reason: 'visual_not_supported' },
    ] }
  const render = (sources) => renderToStaticMarkup(React.createElement(SourceControls, {
    source: { title: '合并整理主题', category: '技术', publish_id: 'page-pub', path: 'knowledge/wiki/page.md',
      current_searchable: true, sources }, busy: false, onAction: async () => {}, onReference() {}, onOpenKnowledge() {},
  }))
  const html = render([first, second, { ...second, ordinal: 7 }])
  assert.equal((html.match(/>查看知识页</g) || []).length, 1)
  assert.equal((html.match(/>查看出处</g) || []).length, 1)
  assert.equal((html.match(/合并整理主题/g) || []).length, 1)
  assert.equal((html.match(/来源管理<\/button>/g) || []).length, 2)
  assert.doesNotMatch(html, /<select|>撤回<|>登记更正</)
  assert.match(html, /原件A.docx/)
  assert.match(html, /原件B.xlsx/)
  assert.match(html, /图像待处理（word\/media\/image.png）/)
  assert.match(html, /图表待处理（xl\/charts\/chart1.xml）/)
  assert.match(html, /暂不支持视觉解析/)
  assert.doesNotMatch(html, /物理页/)
  assert.doesNotMatch(html, /doc-a|doc-b|rev-a|rev-b/)
  const withdrawn = render([first, { ...second, source_status: 'withdrawn', source_state: { ...second.source_state, validity: 'withdrawn' } }])
  assert.equal((withdrawn.match(/来源管理<\/button>/g) || []).length, 2)
  assert.match(withdrawn, /已撤回/)
})
