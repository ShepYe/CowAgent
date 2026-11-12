import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

async function loadStore(name, api = {}) {
  globalThis.localStorage = { getItem: () => null }
  globalThis.editorTestApi = api
  const result = await build({ entryPoints: [`src/renderer/src/store/${name}.ts`], bundle: true, write: false, platform: 'node', format: 'cjs', plugins: [{ name: 'offline', setup(b) {
    b.onResolve({ filter: /(?:i18n|confirmStore|api\/client)$/ }, args => ({ path: args.path, namespace: 'mock' }))
    b.onLoad({ filter: /.*/, namespace: 'mock' }, args => ({ contents: args.path.endsWith('client') ? 'export default globalThis.editorTestApi' : 'export const t=x=>x; export const askConfirm=async()=>false;' }))
  } }] })
  const module = { exports: {} }
  new Function('require', 'module', 'exports', result.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports)
  return module.exports
}
const read = { status: 'success', editable: true, content: '原文\r\n', mtime: 1, sha256: 'server-raw-hash', memory_status: 'unverified', diagnostic: '文件与记忆台账不一致，自动覆盖已停止' }

test('桌面记忆编辑器保留原文和服务哈希，连续保存区分投影状态', async () => {
  const { createDocEditorStore } = await loadStore('docEditorStore')
  const calls = []
  let receipt = { status: 'success', mtime: 2, sha256: 'saved-raw-hash', projected: false }
  const store = createDocEditorStore({ keyOf: d => d, read: async () => read, write: async (...args) => { calls.push(args); return receipt } })
  await store.getState().open('MEMORY.md')
  await store.getState().startEdit()
  assert.equal(store.getState().edit.loaded, '原文\n')
  assert.equal(store.getState().unverified, read.diagnostic)
  store.getState().setDirty(true)
  await store.getState().save('修改', { keepEditing: true })
  assert.equal(calls[0][3], 'server-raw-hash')
  assert.equal(store.getState().projectionPending, true)
  assert.equal(store.getState().edit.error, null)
  receipt = { status: 'success', mtime: 3, sha256: 'next-hash', projected: true }
  store.getState().setDirty(true)
  await store.getState().save('再次修改')
  assert.equal(calls[1][3], 'saved-raw-hash')
  assert.equal(store.getState().projectionPending, false)
})

test('桌面工作区保存沿用哈希，显式覆盖也重新读取并保留CAS', async () => {
  const calls = []
  let currentRead = read
  const { useWorkspaceStore: store } = await loadStore('workspaceStore', { workspaceRead: async () => currentRead, workspaceWrite: async args => { calls.push(args); return { status: 'success', sha256: 'saved-hash', mtime: 2, memory_status: 'pending_projection' } } })
  store.setState({ current: { path: 'MEMORY.md', kind: 'markdown', name: 'MEMORY.md' } })
  await store.getState().startEdit()
  assert.equal(store.getState().edit.loaded, '原文\n')
  assert.equal(store.getState().unverified, read.diagnostic)
  store.getState().setEditDirty(true)
  await store.getState().saveEdit('修改', { keepEditing: true })
  assert.equal(calls[0].expectedHash, read.sha256)
  assert.equal(store.getState().projectionPending, true)
  currentRead = { ...read, sha256: 'latest-server-hash' }
  await store.getState().saveEdit('覆盖', { keepEditing: true, force: true })
  assert.equal(calls[1].expectedHash, 'latest-server-hash')
})

test('Web文档编辑器保留原文、未核验详情及连续保存哈希', async () => {
  let ta, state
  const body = { scrollHeight: 0, clientHeight: 0, querySelector: () => ta, appendChild: el => { ta = el } }
  const context = vm.createContext({ document: { createElement: () => ({ value: '', addEventListener() {}, setSelectionRange() {}, focus() {} }) }, t: x => x, _wsToast() {}, showConfirmDialog() {} })
  vm.runInContext(readFileSync('../channel/web/static/js/doc-editor.js', 'utf8'), context)
  const writes = []
  const editor = context.createDocEditor({ body: () => body, buttons: () => ({}), read: async () => read, write: async (...args) => { writes.push(args); return { status: 'success', mtime: 2, sha256: 'saved-hash', pending_projection: true } }, render() {}, onState: s => { state = s } })
  editor.open({ content: '' })
  await editor.start()
  assert.equal(ta.value, read.content)
  assert.equal(state.unverified, read.diagnostic)
  ta.value = '修改'
  await editor.save({ keepEditing: true })
  assert.equal(writes[0][3], read.sha256)
  assert.equal(state.projectionPending, true)
  ta.value = '再次修改'
  await editor.save({ keepEditing: true })
  assert.equal(writes[1][3], 'saved-hash')
})

test('Web预览编辑器实际显示待完成和未核验，写入保留CAS哈希', async () => {
  let ta
  const title = { classList: { remove() {} } }
  const body = { appendChild: el => { ta = el } }
  const calls = []
  const context = vm.createContext({
    document: { readyState: 'loading', addEventListener() {}, getElementById: id => id === 'ws-preview-content' ? body : id === 'ws-editor' ? ta : id === 'ws-preview-title' ? title : null,
      createElement: () => ({ value: '', addEventListener() {}, focus() {} }) },
    t: x => x, _wsToast() {}, showConfirmDialog() {},
    fetch: async (url, opts) => {
      if (!opts) return { json: async () => read }
      calls.push(JSON.parse(opts.body))
      return { json: async () => ({ status: 'success', mtime: 2, sha256: 'saved-hash', projected: false }) }
    },
  })
  vm.runInContext(readFileSync('../channel/web/static/js/workspace.js', 'utf8'), context)
  vm.runInContext("wsCurrentFile = { path: 'MEMORY.md', name: 'MEMORY.md', kind: 'markdown' };", context)
  await context.startPreviewEdit()
  assert.equal(ta.value, read.content)
  assert.match(title.textContent, /未核验/)
  assert.equal(title.title, read.diagnostic)
  ta.value = '修改'
  await context.savePreviewEdit({ keepEditing: true })
  assert.equal(calls[0].expected_hash, read.sha256)
  assert.match(title.textContent, /投影待完成/)
  ta.value = '再次修改'
  await context.savePreviewEdit({ keepEditing: true })
  assert.equal(calls[1].expected_hash, 'saved-hash')
})
