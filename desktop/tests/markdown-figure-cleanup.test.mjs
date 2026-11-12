import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import React from 'react'

// 执行生产组件的 effect；只替换 React 调度及浏览器/应用边界。
const require = createRequire(import.meta.url)
const output = await build({
  entryPoints: [fileURLToPath(new URL('../src/renderer/src/components/Markdown.tsx', import.meta.url))],
  bundle: true, write: false, format: 'cjs', platform: 'node',
  external: ['react', 'react/jsx-runtime'], loader: { '.css': 'empty' },
  plugins: [{ name: 'effect-boundaries', setup(b) {
    b.onResolve({ filter: /(?:api\/client|store\/workspaceStore|\.\/Lightbox|\.\.\/i18n)$/ }, a => ({ path: a.path, namespace: 'boundary' }))
    b.onLoad({ filter: /.*/, namespace: 'boundary' }, () => ({ contents: `
      export default harness.api;
      export const useLightboxStore = { getState: () => harness.lightbox };
      export const useWorkspaceStore = { getState: () => ({}) };
      export const t = x => x;
    ` }))
  } }],
})

function mount({ image = false, deferred = false } = {}) {
  let effect, cleanup, sequence = 0, resolveFetch
  const urls = [], revoked = [], signals = [], elements = []
  const lightbox = { src: null, close() { this.src = null } }
  const harness = { lightbox, api: {
    getBaseUrl: () => 'https://backend.test', getActiveAgentId: () => 'agent-a',
    getKnowledgeFigureUrl: () => '/figure.png', getKnowledgeFigureOriginalUrl: () => '/original',
  } }
  const element = { dataset: { publishId: 'pub', figureId: 'fig' },
    querySelector: () => ({ textContent: '' }), prepend(img) { elements.push(img) } }
  const root = { querySelectorAll: () => image ? [element] : [] }
  const hooks = { ...React, useRef: () => ({ current: root }), useMemo: f => f(),
    useCallback: f => f, useEffect: f => { effect = f } }
  const response = { ok: true, headers: { get: () => 'image/png' }, blob: async () => ({}) }
  const fetch = (_url, options) => {
    signals.push(options.signal)
    return deferred ? new Promise(resolve => { resolveFetch = () => resolve(response) }) : Promise.resolve(response)
  }
  const module = { exports: {} }
  new Function('require', 'module', 'exports', 'harness', 'document', 'URL', 'fetch', 'IntersectionObserver', output.outputFiles[0].text)(
    name => name === 'react' ? hooks : require(name), module, module.exports, harness,
    { compatMode: 'CSS1Compat', createElement: () => ({}) },
    { createObjectURL: () => { const url = `blob:owned-${++sequence}`; urls.push(url); return url }, revokeObjectURL: url => revoked.push(url) },
    fetch, undefined,
  )
  const render = (props = {}) => {
    cleanup?.()
    element.dataset.loading = ''
    module.exports.default({ content: image ? '![图](knowledge-figure:pub/fig)' : '普通流式消息', ...props })
    cleanup = effect()
  }
  render()
  return { render, unmount: () => cleanup(), lightbox, urls, revoked, signals, elements, resolve: () => resolveFetch() }
}
const settled = () => new Promise(resolve => setImmediate(resolve))

test('普通消息 stream 更新及卸载不关闭别处预览（普通图或受控图）', () => {
  for (const src of ['https://image.test/plain.png', 'blob:another-component']) {
    const view = mount()
    view.lightbox.src = src
    view.render({ content: '新增流式文本' })
    assert.equal(view.lightbox.src, src)
    view.unmount()
    assert.equal(view.lightbox.src, src)
  }
})

test('自身图片离页、内容或 scope 变化时关闭并撤销 URL', async () => {
  for (const change of [null, { content: '新正文' }, { agentId: 'agent-b' }]) {
    const view = mount({ image: true })
    await settled()
    assert.equal(view.elements.length, 1)
    view.lightbox.src = view.urls[0]
    if (change) view.render(change)
    else view.unmount()
    assert.equal(view.lightbox.src, null)
    assert.ok(view.revoked.includes(view.urls[0]))
    view.unmount()
    await settled()
  }
})

test('受控图 cleanup 不关闭已切换到别处的预览，仍撤销自身 URL', async () => {
  const view = mount({ image: true })
  await settled()
  view.lightbox.src = 'blob:other-effect'
  view.unmount()
  assert.equal(view.lightbox.src, 'blob:other-effect')
  assert.deepEqual(view.revoked, view.urls)
  assert.equal(view.signals[0].aborted, true)
})

test('卸载后的迟到响应不生成 URL 或关闭其他预览', async () => {
  const view = mount({ image: true, deferred: true })
  view.lightbox.src = 'https://image.test/other.png'
  view.unmount()
  view.resolve()
  await settled()
  assert.deepEqual(view.urls, [])
  assert.equal(view.lightbox.src, 'https://image.test/other.png')
})
