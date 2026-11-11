import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = await build({ entryPoints: [path.join(root, 'src/renderer/src/lib/knowledgeNavigation.ts')], bundle: true, write: false, platform: 'node', format: 'cjs' })
const module = { exports: {} }
new Function('require', 'module', 'exports', output.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports)
const { resolveKnowledgeLink, isInternalDocumentLink, groupCatalog, legacyKnowledgeTree, scrollToKnowledgeAnchor } = module.exports
const file = (name, title = name) => ({ name, title, size: 1 })
const list = { root_files: [file('index.md'), file('log.md')], tree: [
  { dir: '笔记', files: [file('中文 主题.md'), file('same.md', '根层同名')], children: [
    { dir: '深入', files: [file('same.md', '深层同名'), file('source.md')], children: [] },
  ] },
  { dir: 'other', files: [file('same.md', '其他同名')], children: [] },
  { dir: 'wiki', files: [], children: [] },
  { dir: 'managed', files: [file('old.md')], children: [] },
], stats: { pages: 7, size: 7 }, enabled: true }
const page = { page_id: 'one', page_version: 'v1', path: 'wiki/旧物理分类/中文 页-v1.md', title: '有效主题', category: '当前/手动分类', category_mode: 'manual', category_revision: 3, searchable: true }

test('中文百分号与空格按原文档位置解析，保留查询后的锚点', () => {
  const target = resolveKnowledgeLink(list, '../%E4%B8%AD%E6%96%87%20%E4%B8%BB%E9%A2%98.md?view=1#%E5%85%B3%E9%94%AE%E7%82%B9', '笔记/深入/source.md')
  assert.equal(target.path, '笔记/中文 主题.md')
  assert.equal(target.anchor, '关键点')
  assert.equal(resolveKnowledgeLink(list, './笔记/中文%20主题.md', 'index.md').path, '笔记/中文 主题.md')
})

test('嵌套相对路径及同basename严格匹配，不遍历猜测', () => {
  assert.equal(resolveKnowledgeLink(list, 'same.md', '笔记/深入/source.md').title, '深层同名')
  assert.equal(resolveKnowledgeLink(list, '../same.md', '笔记/深入/source.md').title, '根层同名')
  assert.equal(resolveKnowledgeLink(list, '../../other/same.md', '笔记/深入/source.md').title, '其他同名')
  assert.equal(resolveKnowledgeLink(list, 'same.md', 'index.md'), null)
  assert.equal(resolveKnowledgeLink(list, '../missing/same.md', '笔记/深入/source.md'), null)
  assert.equal(resolveKnowledgeLink(list, '../../../other/same.md', '笔记/深入/source.md'), null)
})

test('knowledge根路径精确定位，分类元数据不会改写真实path', () => {
  const href = `knowledge/${encodeURI(page.path)}#适用范围`
  assert.equal(resolveKnowledgeLink(list, href, '笔记/深入/source.md', [page]).path, page.path)
  assert.equal(resolveKnowledgeLink(list, './wiki/当前/手动分类/中文 页-v1.md', 'index.md', [page]), null)
  assert.equal(resolveKnowledgeLink(list, href, '', [page]).path, page.path)
  assert.equal(resolveKnowledgeLink(list, 'same.md', '', [page]), null)
})

test('内部md及锚点可处理，外部和其他文件链接保持原行为', () => {
  for (const href of ['a.md?x=1#h', './nested/a.MD#h', '#中文', '中文%20页.md']) assert.ok(isInternalDocumentLink(href), href)
  for (const href of ['https://example.com/a.md', '//example.com/a.md', 'mailto:a.md', 'file:///a.md', 'javascript:a.md', '/a.md', 'a.pdf']) {
    assert.equal(isInternalDocumentLink(href), false, href)
    assert.equal(resolveKnowledgeLink(list, href, 'index.md'), null)
  }
  assert.equal(resolveKnowledgeLink(list, './%ZZ.md', 'index.md'), null)
  assert.equal(resolveKnowledgeLink(list, '#section', '笔记/深入/source.md').anchor, 'section')
})

test('当前目录按effectivecategory过滤分组，不改变不可变物理路径', () => {
  const other = { ...page, page_id: 'two', category: '另一分类', title: '另一个主题', path: 'wiki/原分类/two.md' }
  assert.deepEqual(groupCatalog([page, other, { ...other, page_id: 'paused', searchable: false }], '手动').map(([category, pages]) => [category, pages.map(p => p.path)]), [[page.category, [page.path]]])
  assert.equal(groupCatalog([page, other]).flatMap(([, pages]) => pages).length, 2)
  assert.equal(page.path, 'wiki/旧物理分类/中文 页-v1.md')
  const legacy = legacyKnowledgeTree(list)
  assert.ok(legacy.tree.some(d => d.dir === '笔记'))
  assert.ok(legacy.tree.some(d => d.dir === 'managed'))
  assert.ok(!legacy.tree.some(d => d.dir === 'wiki'))
  assert.deepEqual(legacy.root_files, list.root_files)
})

test('锚点滚动按id或中文标题匹配，未找到返回false', () => {
  const calls = []
  const heading = { id: '中文-主题', textContent: '中文 主题', scrollIntoView: value => calls.push(value) }
  assert.equal(scrollToKnowledgeAnchor({ querySelectorAll: () => [heading] }, '中文 主题'), true)
  assert.deepEqual(calls, [{ block: 'start' }])
  assert.equal(scrollToKnowledgeAnchor({ querySelectorAll: () => [heading] }, 'missing'), false)
})
