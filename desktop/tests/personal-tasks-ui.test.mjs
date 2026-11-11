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
    builder.onResolve({ filter: /^\.\.\// }, ({ path }) => ({ path, namespace: 'offline' }))
    builder.onLoad({ filter: /.*/, namespace: 'offline' }, () => ({ contents: 'export default function Stub() { return null }; export const askConfirm = async () => false;' }))
  } }],
})
const module = { exports: {} }
new Function('require', 'module', 'exports', result.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports)
const { personalOutcomeLabel, personalKnowledgeFingerprint, personalTaskTitle, SourceControls } = module.exports
const published = (...refs) => ({ outcome: 'published', result_refs: refs })

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
  assert.equal((html.match(/>撤回</g) || []).length, 2)
  assert.equal((html.match(/>登记更正</g) || []).length, 2)
  assert.equal((html.match(/>修订用途</g) || []).length, 2)
  assert.match(html, /原件A.docx/)
  assert.match(html, /原件B.xlsx/)
  assert.match(html, /图像待处理（word\/media\/image.png）/)
  assert.match(html, /图表待处理（xl\/charts\/chart1.xml）/)
  assert.match(html, /暂不支持视觉解析/)
  assert.doesNotMatch(html, /物理页/)
  assert.doesNotMatch(html, /doc-a|doc-b|rev-a|rev-b/)
  const withdrawn = render([first, { ...second, source_status: 'withdrawn', source_state: { ...second.source_state, validity: 'withdrawn' } }])
  assert.equal((withdrawn.match(/>撤回</g) || []).length, 1)
  assert.match(withdrawn, /已撤回/)
})
