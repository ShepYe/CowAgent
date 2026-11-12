import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = await build({
  entryPoints: [path.join(root, 'src/renderer/src/components/Markdown.tsx')],
  bundle: true, write: false, platform: 'node', format: 'cjs',
  external: ['react', 'react/jsx-runtime'], loader: { '.css': 'empty' },
  plugins: [{ name: 'isolate-app-services', setup(build) {
    build.onResolve({ filter: /(?:api\/client|store\/workspaceStore|\.\/Lightbox|\.\.\/i18n)$/ }, args => ({ path: args.path, namespace: 'app-stub' }))
    build.onLoad({ filter: /.*/, namespace: 'app-stub' }, () => ({ contents: `
      export default { getFileUrl: x => x, getServeFileUrl: x => x };
      export const t = x => x;
      export const useWorkspaceStore = { getState: () => ({}) };
      export const useLightboxStore = { getState: () => ({}) };
    ` }))
  } }],
})
const module = { exports: {} }
new Function('require', 'module', 'exports', output.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports)
const { markdownToHtml, default: Markdown } = module.exports
const matrix = String.raw`T=\begin{bmatrix}1 & 2 \\ 3 & 4\end{bmatrix},`

test('未闭合公式的扫描量随输入长度线性增长', () => {
  // 统计字符串扫描量，不用墙钟耗时；finally 恢复原型，避免影响其他测试。
  const indexOf = String.prototype.indexOf
  const trim = String.prototype.trim
  for (const count of [512, 1024]) {
    for (const source of [
      '\\[\n\n'.repeat(count),
      '\\[\n\n'.repeat(count) + '\\] 尾随正文',
      '正文 ' + '\\(x '.repeat(count),
      '正文 ' + ('\\(x '.repeat(32) + '\n').repeat(count / 32) + '\\)',
    ]) {
      let scanned = 0
      let html
      try {
        String.prototype.indexOf = function (needle, from = 0) {
          const result = indexOf.call(this, needle, from)
          if (['\\]', '\\)', '$$', '$', '\n'].includes(needle)) {
            scanned += (result < 0 ? this.length : result + needle.length) - from
          }
          return result
        }
        String.prototype.trim = function () {
          scanned += Math.max(1, this.length)
          return trim.call(this)
        }
        html = markdownToHtml(source)
      } finally {
        String.prototype.indexOf = indexOf
        String.prototype.trim = trim
      }
      assert.doesNotMatch(html, /class="katex"/)
      assert.ok(scanned <= source.length * 40, `${source.length} 字符扫描了 ${scanned} 字符`)
    }
  }
})

test('失败缓存不遮蔽边界行的新公式，且不跨容器或文档复用', () => {
  assert.doesNotMatch(markdownToHtml('\\[\n\n\\[\n\n正文'), /class="katex"/)
  for (const source of [
    '$$\n$$x\n$$',
    '> $$\n> $$x\n> $$',
    '- 开头\n\n  $$\n  $$x\n  $$',
    '\\[x\\]',
    '正文 \\(x\\)',
  ]) {
    const html = markdownToHtml(source)
    assert.equal((html.match(/class="katex"/g) || []).length, 1, source)
    assert.doesNotMatch(html, /math-fallback/)
  }
})

for (const [name, source] of [
  ['方括号块公式', `\\[\n${matrix}\n\\]`],
  ['圆括号行内公式', String.raw`变换为 \(T_{ij}=x^2\)。`],
  ['双美元块公式', `$$\n${matrix}\n$$`],
  ['单行方括号矩阵', `\\[${matrix}\\]`],
  ['正文中的显示公式', `矩阵 \\[${matrix}\\] 结束。`],
  ['美元行内公式', '变换为 $T_{ij}=x^2$。'],
]) {
  test(`${name}通过真实组件和HTML导出生成公式`, () => {
    for (const html of [markdownToHtml(source), renderToStaticMarkup(React.createElement(Markdown, { content: source, onInternalLink() {} }))]) {
      assert.match(html, /class="katex"/, html)
      assert.match(html, /<math[\s>]/)
      assert.doesNotMatch(html, /katex-error/)
      if (source.includes('bmatrix')) {
        assert.match(html, /<mtable/)
        assert.equal((html.match(/<mtr>/g) || []).length, 2, '矩阵的两个行分隔符保留到数学解析阶段')
      }
    }
  })
}

test('多行矩阵、空行及引用/列表容器均保留TeX内容', () => {
  const source = String.raw`\[
T=\begin{bmatrix}
1 & 2 \\

3 & 4
\end{bmatrix}
\]`
  for (const input of [source, source.replaceAll('\\[', () => '$$').replaceAll('\\]', () => '$$'), source.split('\n').map(line => `> ${line}`).join('\n'), `- 变换\n\n${source.split('\n').map(line => `  ${line}`).join('\n')}`]) {
    const html = markdownToHtml(input)
    assert.match(html, /<mtable/)
    assert.match(html, /class="katex-display"/)
    assert.doesNotMatch(html, /math-fallback/)
  }
})

test('代码中的公式分隔符保持原文，金额、转义和未闭合公式不误识别', () => {
  for (const input of [
    '`\\[x^2\\]`', '```tex\n\\[x^2\\]\n```', '    $$x^2$$',
    '价格 $5 和 $10。', String.raw`\$x\$ 和 \\[x\\]`, '$x', String.raw`\[x`,
    '[T=普通方括号]',
  ]) assert.doesNotMatch(markdownToHtml(input), /class="katex"/, input)
  const code = markdownToHtml('`' + matrix + '`')
  assert.ok(code.includes(matrix.replaceAll('&', '&amp;')), code)
})

test('非法TeX安全回退，后续公式正常渲染，宏定义不跨公式泄漏', () => {
  const html = markdownToHtml(String.raw`\(\unknowncommand{<script>alert(1)</script>}\) 后续 $x^2$`)
  assert.ok(html.includes('\\unknowncommand'))
  assert.match(html, /&lt;script&gt;/)
  assert.doesNotMatch(html, /<script>/)
  assert.match(html, /class="katex"/)
  assert.doesNotMatch(markdownToHtml(String.raw`$\href{javascript:alert(1)}{x}$`), /href="javascript:/)
  markdownToHtml(String.raw`$\gdef\privateMacro{x}\privateMacro$`)
  assert.ok(markdownToHtml(String.raw`$\privateMacro$`).includes('\\privateMacro'))
})

test('相邻公式、普通Markdown和知识链接保留既有行为', () => {
  const html = renderToStaticMarkup(React.createElement(Markdown, {
    content: '# 中文标题\n\n是**"引号"**—— $x_1$、\\(y^2\\)\n\n[知识](next.md#标题)\n\n| 列 | 公式 |\n|---|---|\n| A | $x$ |',
    onInternalLink() {},
  }))
  assert.match(html, /id="中文标题"/)
  assert.match(html, /<strong>/)
  assert.match(html, /href="next.md#/)
  assert.match(html, /class="table-wrap"/)
  assert.equal((html.match(/class="katex"/g) || []).length, 3)
})

test('核验仓库web对应实现也将矩阵公式输出为原始文本', () => {
  const context = vm.createContext({
    window: {}, document: { addEventListener() {} },
    escapeHtml: text => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  })
  vm.runInContext(readFileSync(path.join(root, '../channel/web/static/vendor/markdown-it/markdown-it.min.js'), 'utf8'), context)
  vm.runInContext('window.markdownit = markdownit', context)
  vm.runInContext(readFileSync(path.join(root, '../channel/web/static/js/core/markdown.js'), 'utf8'), context)
  context.source = `\\[${matrix}\\]`
  const html = vm.runInContext('md.render(source)', context)
  assert.doesNotMatch(html, /class="katex"/)
  assert.ok(html.includes('\\begin{bmatrix}'), html)
  assert.ok(html.includes('[T='), html)
})
