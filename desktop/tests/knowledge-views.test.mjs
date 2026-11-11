import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const require = createRequire(import.meta.url)
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
async function bundle(entry, extra = '', pageMode = false) {
  const filename = path.join(root, 'src/renderer/src', entry)
  const output = await build({ stdin: { contents: await readFile(filename, 'utf8') + extra, resolveDir: path.dirname(filename), loader: filename.endsWith('tsx') ? 'tsx' : 'ts' },
    bundle: true, write: false, format: 'cjs', platform: 'node', external: ['react', 'react/jsx-runtime'], plugins: [{ name: 'offline-app', setup(b) {
      if (pageMode) {
        b.onResolve({ filter: /^(react-router-dom|.*(?:store\/(?:agentStore|docEditorStore|confirmStore)|components\/(?:KnowledgeGraph|AgentScopeSelect|DocEditor|Tooltip)|\.\/PersonalTasksPanel))$/ }, a => ({ path: a.path, namespace: 'page-stub' }))
        b.onLoad({ filter: /.*/, namespace: 'page-stub' }, a => {
          if (a.path.endsWith('agentStore')) return { contents: 'export const selectMultiAgent=s=>s.multiAgent; export const useAgentStore=fn=>fn(globalThis.__knowledgeAgents);' }
          if (a.path.endsWith('docEditorStore')) return { contents: 'export const createDocEditorStore=()=>globalThis.__knowledgeEditor;' }
          if (a.path.endsWith('confirmStore')) return { contents: 'export const askConfirm=async()=>true;' }
          if (a.path === 'react-router-dom') return { contents: 'export const useNavigate=()=>()=>{};' }
          if (a.path.endsWith('DocEditor')) return { contents: 'export const DocActions=()=>null, DocEditor=()=>null, DocNotice=()=>null, DocView=()=>null;' }
          const name = a.path.split('/').pop()
          return { contents: `export default function ${name}(){return null}` }
        })
      }
      b.onResolve({ filter: /(?:api\/client|\/i18n|store\/workspaceStore|\.\/Lightbox)$/ }, a => ({ path: a.path, namespace: 'stub' }))
      b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: `export default globalThis.__knowledgeApi; export const t=x=>x,tf=x=>x,getLang=()=>"zh"; export const useWorkspaceStore={getState:()=>({openLink:()=>globalThis.__workspaceOpened=true})}; export const useLightboxStore={getState:()=>({open:()=>{}})};` }))
    } }] })
  const module = { exports: {} }
  new Function('require', 'module', 'exports', output.outputFiles[0].text)(require, module, module.exports)
  return module.exports
}
const requests = []
globalThis.__knowledgeApi = {
  getKnowledgeHistory: async (...args) => { requests.push(['history', ...args]); return { status: 'success', events: [], total: 41, has_more: args[0] === 0, legacy_content: '旧记录保留' } },
  getKnowledgeVersion: async (...args) => { requests.push(['version', ...args]); return { status: 'success', version: version } },
}
const navigation = await bundle('lib/knowledgeNavigation.ts')
const catalog = await bundle('components/KnowledgeCatalog.tsx')
const history = await bundle('components/KnowledgeHistory.tsx')
const versions = await bundle('components/KnowledgeVersionView.tsx')
const category = await bundle('components/KnowledgeCategoryMenu.tsx', '\nexport { CategoryDialog };')
const markdown = await bundle('components/Markdown.tsx')
const page = { page_id: 'p', page_version: 'old', title: '中文主题', category: '有效分类', category_mode: 'manual', category_revision: 7, path: 'wiki/物理分类/中文 页-old.md', searchable: true }
const version = { ...page, publish_id: 'old', content: '# 中文主题\n\n正文\n\n## 范围\n可读证据', created_at: '2026-11-11T09:00:00Z', current_status: 'superseded', searchable: false, current_path: 'wiki/新目录/current.md' }

// 在Node内驱动真实组件钩子与事件；不启动浏览器、网络或真实桌面。
function harness(Component, props) {
  const slots = []; let cursor = 0; let effects = []; let tree
  const dispatcher = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value }] },
    useRef(initial) { const i = cursor++; return slots[i] ||= { current: initial } },
    useMemo(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || deps.some((d, j) => !Object.is(d, old.deps[j]))) slots[i] = { deps, value: fn() }; return slots[i].value },
    useCallback(fn, deps) { return this.useMemo(() => fn, deps) },
    useEffect(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || deps.some((d, j) => !Object.is(d, old.deps[j]))) { effects.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: fn() } }) } },
  }
  const internals = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED
  return { render() { cursor = 0; const previous = internals.ReactCurrentDispatcher.current; internals.ReactCurrentDispatcher.current = dispatcher
    try { tree = Component(props) } finally { internals.ReactCurrentDispatcher.current = previous }
    return tree }, flushEffects() { const pending = effects; effects = []; pending.forEach(fn => fn()) },
    unmount() { slots.forEach(slot => slot?.cleanup?.()) }, get tree() { return tree } }
}
function nodes(tree) { if (!tree || typeof tree !== 'object') return []; if (Array.isArray(tree)) return tree.flatMap(nodes); return [tree, ...nodes(tree.props?.children)] }
function text(tree) { if (typeof tree === 'string' || typeof tree === 'number') return String(tree); if (Array.isArray(tree)) return tree.map(text).join(''); return tree?.props ? text(tree.props.children) : '' }
const settle = () => new Promise(resolve => setImmediate(resolve))

test('有效目录显示分类元数据及手动锁，不使用物理目录作为分类', () => {
  const html = renderToStaticMarkup(React.createElement(catalog.default, { pages: [page], onOpen() {} }))
  assert.match(html, /有效分类/); assert.match(html, /中文主题/); assert.match(html, /手动分类/)
  assert.doesNotMatch(html, /物理分类|中文 页-old/)
  const opened = []; const h = harness(catalog.KnowledgeCatalogGroups, { pages: [page], onOpen: (...a) => opened.push(a) })
  const group = nodes(h.render()).find(node => typeof node.type === 'function')
  const gh = harness(group.type, group.props)
  nodes(gh.render()).find(node => node.type === 'button' && text(node).includes('中文主题')).props.onClick()
  assert.deepEqual(opened, [[page.path, page.title]])
})

test('目录无原索引内容时隐藏入口，旧未托管文字和链接可查看', () => {
  for (const legacyContent of [undefined, '', ' \n\t']) {
    const html = renderToStaticMarkup(React.createElement(catalog.default, { pages: [page], onOpen() {}, legacyContent }))
    assert.doesNotMatch(html, /<details|原索引与说明/)
    assert.equal((html.match(/知识索引/g) || []).length, 1)
  }
  const hits = []
  const legacyContent = '旧资料说明\n\n[旧知识](旧目录/中文%20页.md)'
  const h = harness(catalog.default, { pages: [page], onOpen() {}, legacyContent, onInternalLink: href => hits.push(href) })
  const tree = h.render()
  const html = renderToStaticMarkup(tree)
  assert.match(html, /<details/); assert.match(html, /原索引与说明/); assert.match(html, /旧资料说明/)
  const md = nodes(tree).find(n => typeof n.type === 'function' && n.props.content === legacyContent)
  md.props.onInternalLink('旧目录/中文%20页.md')
  assert.deepEqual(hits, ['旧目录/中文%20页.md'])
})

test('真实事件打开对应publish_id，失效历史不标可检索', () => {
  const events = [{ id: 'event', event_type: 'updated', created_at: version.created_at, publish_id: 'old', title: page.title, category: page.category, current_status: 'superseded', searchable: false }]
  const html = renderToStaticMarkup(React.createElement(history.KnowledgeEventRows, { events, onOpenVersion() {} }))
  assert.match(html, /更新知识/); assert.match(html, /历史版本/); assert.match(html, /当前不可检索/)
  const opened = []; const tree = history.KnowledgeEventRows({ events, onOpenVersion: id => opened.push(id) })
  nodes(tree).find(n => n.type === 'button').props.onClick(); assert.deepEqual(opened, ['old'])
})

test('时间线分页向API传实际offset，原记录保留且不补造事件', async () => {
  requests.length = 0
  const h = harness(history.default, { agentId: 'scoped', revision: 1, onOpenVersion() {}, onInternalLink() {} })
  h.render(); h.flushEffects(); await settle(); h.render()
  assert.deepEqual(requests[0], ['history', 0, 20, 'scoped'])
  const next = nodes(h.tree).find(n => n.type === 'button' && text(n).includes('下一页'))
  next.props.onClick(); h.render(); h.flushEffects(); await settle(); h.render()
  assert.deepEqual(requests[1], ['history', 20, 20, 'scoped'])
  assert.match(text(h.tree), /原有整理记录/)
  assert.equal(nodes(h.tree).find(n => n.type === history.KnowledgeEventRows).props.events.length, 0)
  h.unmount()
})

test('历史版本独立使用version接口，不走当前文件读取', async () => {
  requests.length = 0
  const h = harness(versions.default, { publishId: 'old', agentId: 'scoped', onBack() {}, onOpenCurrent() {}, onInternalLink() {} })
  h.render(); h.flushEffects(); await settle(); h.render()
  assert.deepEqual(requests, [['version', 'old', 'scoped']])
  assert.equal(nodes(h.tree).find(n => n.type === versions.KnowledgeVersionContent).props.version, version)
  h.unmount()
})

test('历史标题仅展示一次，正文不变，当前版入口使用真实path', () => {
  const calls = []
  const tree = versions.KnowledgeVersionContent({ version, onOpenCurrent: (...args) => calls.push(args), onInternalLink() {} })
  const md = nodes(tree).find(n => typeof n.type === 'function' && typeof n.props.content === 'string')
  assert.doesNotMatch(md.props.content, /^# 中文主题/); assert.match(md.props.content, /正文/)
  assert.match(version.content, /^# 中文主题/)
  const html = renderToStaticMarkup(tree)
  assert.match(html, /历史版本.*只读/); assert.match(html, /当前不可检索/)
  assert.equal((html.match(/中文主题/g) || []).length, 1)
  nodes(tree).find(n => n.type === 'button').props.onClick()
  assert.deepEqual(calls, [[version.current_path, version.title]])
  const paused = renderToStaticMarkup(React.createElement(versions.KnowledgeVersionContent, { version: { ...version, current_status: 'paused', current_path: undefined }, onOpenCurrent() {}, onInternalLink() {} }))
  assert.doesNotMatch(paused, /查看当前有效版/)
})

test('分类使用页面版本与分类修订，恢复自动不提交新分类或文件路径', () => {
  assert.deepEqual(navigation.pageCategoryAction(page, '新分类'), { action: 'set_page_category', payload: { page_id: 'p', expected_version: 'old', expected_category_revision: 7, category: '新分类' } })
  assert.deepEqual(navigation.pageCategoryAction(page), { action: 'reset_page_category', payload: { page_id: 'p', expected_version: 'old', expected_category_revision: 7 } })
})

test('分类弹窗已有分类可键盘激活，新分类Enter提交，Esc及Tab陷阱可用', async () => {
  const changes = []; let closed = 0
  const h = harness(category.CategoryDialog, { page, categories: ['有效分类', '另一分类'], onChange: async (...args) => changes.push(args), onClose: () => closed++ })
  let tree = h.render()
  assert.ok(!nodes(tree).some(n => n.type === 'select' || n.type === 'option'))
  const choice = nodes(tree).find(n => n.type === 'button' && text(n) === '另一分类')
  assert.equal(choice.props.type, 'button'); assert.equal(typeof choice.props.onClick, 'function')
  choice.props.onClick(); tree = h.render()
  assert.equal(nodes(tree).find(n => n.type === 'input').props.value, '另一分类')
  nodes(tree).find(n => n.type === 'input').props.onChange({ target: { value: '新建/分类' } }); tree = h.render()
  nodes(tree).find(n => n.type === 'form').props.onSubmit({ preventDefault() {} }); await settle()
  assert.deepEqual(changes, [[page, '新建/分类']]); assert.equal(closed, 1)
  const fresh = harness(category.CategoryDialog, { page, categories: [], onChange: async () => {}, onClose: () => closed++ })
  tree = fresh.render(); const dialog = nodes(tree).find(n => n.props.role === 'dialog')
  let focus = ''; let prevented = 0
  const first = { focus: () => { focus = 'first' } }, last = { focus: () => { focus = 'last' } }
  dialog.ref.current = { querySelectorAll: () => [first, last] }
  const previousDocument = globalThis.document
  try {
    globalThis.document = { activeElement: last }
    dialog.props.onKeyDown({ key: 'Tab', shiftKey: false, preventDefault: () => prevented++ })
    assert.equal(focus, 'first')
    globalThis.document.activeElement = first
    dialog.props.onKeyDown({ key: 'Tab', shiftKey: true, preventDefault: () => prevented++ })
    assert.equal(focus, 'last')
    dialog.props.onKeyDown({ key: 'Escape', preventDefault: () => prevented++, stopPropagation() {} })
    assert.equal(closed, 2); assert.equal(prevented, 3)
  } finally { globalThis.document = previousDocument }
})

test('分类入口关闭后恢复触发焦点', () => {
  const h = harness(category.default, { page, categories: [], onChange: async () => {} })
  let tree = h.render(); const trigger = nodes(tree).find(n => n.type === 'button'); let focused = false
  trigger.ref.current = { focus: () => { focused = true } }
  trigger.props.onClick(); tree = h.render()
  nodes(tree).find(n => typeof n.type === 'function').props.onClose()
  assert.equal(focused, true)
})

test('Markdown内部md含query/anchor使用回调，其他页面与external不被截获', () => {
  const hits = []; const href = './中文%20页.md?mode=1#位置'
  const link = { getAttribute: () => href }
  const target = { closest: selector => selector === 'a' ? link : null }
  let prevented = false
  const h = harness(markdown.default, { content: `[中文](${href})`, onInternalLink: h => hits.push(h) })
  const tree = h.render()
  tree.props.onClick({ target, preventDefault: () => { prevented = true } })
  assert.ok(prevented); assert.deepEqual(hits, [href])
  const external = harness(markdown.default, { content: '[外链](https://example.test/doc.md)', onInternalLink: h => hits.push(h) }).render()
  external.props.onClick({ target: { closest: selector => selector === 'a' ? { getAttribute: () => 'https://example.test/doc.md' } : null }, preventDefault: () => assert.fail('不能截获外链') })
  const ordinary = harness(markdown.default, { content: '[原链接](doc.md)' }).render()
  ordinary.props.onClick({ target: { closest: () => null }, preventDefault: () => assert.fail('无回调保持原行为') })
  assert.match(external.props.dangerouslySetInnerHTML.__html, /target="_blank"/)
})

test('旧不可变related链接只规范化完整path，不按basename猜当前', () => {
  const address = navigation.knowledgeLinkAddress('../旧分类/旧%20知识-old.md?x=1#原结论', 'wiki/新分类/current.md')
  assert.deepEqual(address, { path: 'wiki/旧分类/旧 知识-old.md', anchor: '原结论' })
  assert.equal(navigation.resolveKnowledgeLink({ tree: [], root_files: [], stats: {}, enabled: true }, '../旧分类/旧%20知识-old.md', 'wiki/新分类/current.md', [page]), null)
})

// 直接驱动 KnowledgePage，API 与编辑器只在内存中替身；覆盖跨组件异步完成顺序。
let editorState
globalThis.__knowledgeEditor = Object.assign(selector => selector(editorState), {
  getState: () => editorState, subscribe: () => () => {},
})
const knowledgePage = await bundle('pages/KnowledgePage.tsx', '', true)
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
async function pageHarness() {
  globalThis.localStorage = { getItem: () => 'A', setItem() {} }
  globalThis.__knowledgeAgents = { multiAgent: true, defaultAgentId: 'A', agents: [{ id: 'A' }, { id: 'B' }] }
  editorState = { doc: null, content: '', loading: false, edit: null, readonly: null,
    open: async doc => { editorState.doc = doc }, close: async () => { editorState.doc = null; return true },
    forget: () => { editorState.doc = null }, guard: async () => true }
  Object.assign(globalThis.__knowledgeApi, {
    setBaseUrl() {},
    getKnowledgeList: async () => ({ enabled: true, root_files: [{ name: 'index.md', title: '知识索引' }], tree: [], stats: { pages: 1, size: 1 } }),
    getKnowledgeCatalog: async () => ({ status: 'success', pages: [page], categories: [page.category] }),
    getKnowledgeGraph: async () => ({ nodes: [], links: [] }),
  })
  const h = harness(knowledgePage.default, { baseUrl: 'offline' })
  h.render(); h.flushEffects(); await settle(); h.render(); h.flushEffects(); await settle(); h.render()
  return h
}
const findComponent = (tree, name) => nodes(tree).find(n => typeof n.type === 'function' && n.type.name === name)

test('分类冲突刷新后仍显示错误，弹窗保留并用最新双修订重试', async () => {
  const h = await pageHarness(); const actions = []; let metadata = page
  editorState.doc = { path: page.path, title: page.title, agentId: 'A' }
  globalThis.__knowledgeApi.getKnowledgeCatalog = async () => ({ status: 'success', pages: [metadata], categories: [metadata.category] })
  globalThis.__knowledgeApi.knowledgeAction = async action => {
    actions.push(action)
    metadata = { ...page, category_revision: 8, category: '另一会话分类' }
    return actions.length === 1 ? { status: 'error', message: '分类修订冲突，请核对后重试' } : { status: 'success' }
  }
  h.render()
  let entry = findComponent(h.tree, 'KnowledgeCategoryMenu')
  const menuProps = { ...entry.props }; const menu = harness(entry.type, menuProps)
  nodes(menu.render()).find(n => n.type === 'button').props.onClick(); menu.render()
  let dialogNode = findComponent(menu.tree, 'CategoryDialog'); const initialKey = dialogNode.key
  const dialogProps = { ...dialogNode.props }; const dialog = harness(dialogNode.type, dialogProps)
  nodes(dialog.render()).find(n => n.type === 'form').props.onSubmit({ preventDefault() {} })
  await settle(); h.render()
  assert.match(text(h.tree), /分类修订冲突/)
  entry = findComponent(h.tree, 'KnowledgeCategoryMenu'); Object.assign(menuProps, entry.props)
  menu.render(); dialogNode = findComponent(menu.tree, 'CategoryDialog')
  assert.equal(dialogNode.key, initialKey, '元数据刷新不能重建正在显示冲突的弹窗')
  Object.assign(dialogProps, dialogNode.props); dialog.render()
  assert.match(text(nodes(dialog.tree).find(n => n.props.role === 'alert')), /分类修订冲突/)
  assert.equal(dialogProps.page.category_revision, 8)
  nodes(dialog.tree).find(n => n.type === 'form').props.onSubmit({ preventDefault() {} })
  await settle()
  assert.deepEqual(actions.map(a => a.payload.expected_category_revision), [7, 8])
  assert.deepEqual(actions.map(a => a.payload.expected_version), ['old', 'old'])
  menu.render()
  assert.equal(findComponent(menu.tree, 'CategoryDialog'), undefined, '成功重试后正常关闭')
  h.unmount(); menu.unmount(); dialog.unmount()
})

test('返回目录使待返回历史path请求失效，不被迟到响应重新打开', async () => {
  const h = await pageHarness(); const pending = deferred()
  globalThis.__knowledgeApi.getKnowledgeVersionByPath = () => pending.promise
  const catalogNode = findComponent(h.tree, 'KnowledgeCatalog')
  const opening = catalogNode.props.onInternalLink('wiki/旧目录/归档.md#结论')
  nodes(h.tree).find(n => n.type === 'button' && text(n).includes('返回目录')).props.onClick()
  await settle(); h.render()
  assert.equal(editorState.doc, null)
  pending.resolve({ status: 'success', version: { publish_id: 'late', path: 'wiki/旧目录/归档.md' } })
  await opening; h.render()
  assert.equal(findComponent(h.tree, 'KnowledgeVersionView'), undefined)
  assert.equal(editorState.doc, null)
  h.unmount()
})

for (const oldFails of [false, true]) for (const oldAfterB of [false, true]) test(`图谱切换Agent后丢弃旧${oldFails ? '失败' : '成功'}响应（${oldAfterB ? 'B先完成' : 'A先完成'}）`, async () => {
  const h = await pageHarness(); const a = deferred(), b = deferred(); const calls = []
  globalThis.__knowledgeApi.getKnowledgeGraph = id => { calls.push(id); return id === 'A' ? a.promise : b.promise }
  nodes(h.tree).find(n => n.props.label === 'knowledge_tab_graph').props.onClick()
  await settle(); h.render(); h.flushEffects(); await settle(); h.render()
  assert.deepEqual(calls, ['A'])
  const scope = findComponent(h.tree, 'AgentScopeSelect'); await scope.props.onChange('B'); await settle()
  h.render(); h.flushEffects(); await settle(); h.render(); h.flushEffects(); await settle(); h.render()
  assert.deepEqual(calls, ['A', 'B'])
  const expected = { nodes: [{ id: 'B-current' }], links: [] }
  if (oldAfterB) { b.resolve(expected); await settle(); h.render(); h.flushEffects() }
  if (oldFails) a.reject(new Error('A迟到失败'))
  else a.resolve({ nodes: [{ id: 'A-old' }], links: [] })
  await settle(); h.render(); h.flushEffects()
  if (!oldAfterB) assert.equal(findComponent(h.tree, 'KnowledgeGraph'), undefined, 'B仍加载中，不能呈现A或清掉B loading')
  b.resolve(expected); await settle(); h.render(); h.flushEffects()
  assert.deepEqual(findComponent(h.tree, 'KnowledgeGraph').props.data, expected)
  assert.deepEqual(calls, ['A', 'B'])
  h.unmount()
})
