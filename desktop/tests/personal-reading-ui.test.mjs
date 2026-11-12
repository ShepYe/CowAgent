import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { fileURLToPath } from 'node:url'

const calls = []
globalThis.__readingCopyCalls = calls
const result = await build({
  entryPoints: [fileURLToPath(new URL('../src/renderer/src/pages/PersonalTasksPanel.tsx', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs', external: ['react', 'react/jsx-runtime'],
  plugins: [{ name: 'offline-reading', setup(builder) {
    builder.onResolve({ filter: /(?:api\/client|components\/(?:Markdown|Tooltip)|store\/confirmStore|i18n)$/ }, ({ path }) => ({ path, namespace: 'offline' }))
    builder.onLoad({ filter: /.*/, namespace: 'offline' }, ({ path }) => ({ contents: path.endsWith('api/client')
      ? 'export default {personalReadingCopy: (...args) => { globalThis.__readingCopyCalls.push(args); return "/authorized-copy#page=" + args[3] }};'
      : 'export default function Stub() { return null }; export const askConfirm = async () => false; export const t = key => key;' }))
  } }],
})
const module = { exports: {} }
new Function('require', 'module', 'exports', result.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports)
const { ReadingProgress, ReadingCopyLink, observationLabel } = module.exports
const markup = (component, props) => renderToStaticMarkup(React.createElement(component, props))

test('prepared/unknown 仅显示待补读，部分发布不显示完成', () => {
  const task = { status: 'running', reading_progress: { targets: ['r:4', 'r:8'], delivered: [],
    prepared: ['r:4'], unknown: ['r:8'], pending: ['r:4', 'r:8'], published: ['r:4'], remaining: ['r:8'], task_complete: false } }
  const html = markup(ReadingProgress, { task })
  assert.match(html, /已送达 0\/2 页/)
  assert.match(html, /待补读 2 页/)
  assert.match(html, /部分已发布/)
  assert.doesNotMatch(html, />整理完成</)
  assert.match(html, /title="[^"]*物理页 4、8/)
  assert.match(html, /结果未知：r：物理页 8/)
  assert.equal(markup(ReadingProgress, { task: { status: 'succeeded' } }), '')
})

test('模型转写和解释不冒充原文文字', () => {
  assert.equal(observationLabel({ type: 'ocr', origin: 'model' }), '模型图中文字')
  assert.equal(observationLabel({ type: 'visual_interpretation', origin: 'model' }), '模型视觉解释')
  assert.equal(observationLabel({ reading_id: 'rid', type: 'future' }), '模型观察')
  assert.equal(observationLabel({ type: 'text' }), '原文文字')
})

test('原物理页映射到副本批内页，下载只传授权键', () => {
  calls.length = 0
  const reference = { ordinal: 3, location: { kind: 'pdf', physical_page: 9 }, reading_copy: { page_map: [8, 9, 10] } }
  const html = markup(ReadingCopyLink, { reference, taskId: 'task-1', publishId: 'publish-1' })
  assert.deepEqual(calls, [['task-1', 'publish-1', 3, 2]])
  assert.match(html, /#page=2/)
  assert.match(html, /物理页 9/)
  assert.equal(markup(ReadingCopyLink, { reference: { ...reference, location: { physical_page: 2 } }, taskId: 't', publishId: 'p' }), '')
})

test('不可核验台账不显示已读', () => {
  const html = markup(ReadingProgress, { task: { reading_progress: { unavailable: true } } })
  assert.match(html, /阅读状态不可用/)
  assert.doesNotMatch(html, /已送达|整理完成/)
})

test('Office副本使用Word副本页或源幻灯片，不显示原件物理页', () => {
  for (const location of [
    { kind: 'word_reading_copy_page', reading_copy_page: 3 },
    { kind: 'slide', source_slide_id: '258', source_slide_number: 3 },
  ]) {
    calls.length = 0
    const reference = { ordinal: 0, location, reading_copy: { page_map: [2, 3], source_page_map: [{ kind: 'other' }, location] } }
    const html = markup(ReadingCopyLink, { reference, taskId: 'office', publishId: 'pub' })
    assert.deepEqual(calls, [['office', 'pub', 0, 2]])
    assert.match(html, /#page=2/)
    assert.doesNotMatch(html, /物理页/)
    assert.match(html, location.kind === 'slide' ? /幻灯片 3/ : /Word阅读副本页 3/)
  }
})
