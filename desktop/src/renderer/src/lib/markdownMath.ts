import type MarkdownIt from 'markdown-it'
import katex from 'katex'

const delimiters = [
  { open: '\\[', close: '\\]', display: true },
  { open: '\\(', close: '\\)', display: false },
  { open: '$$', close: '$$', display: true },
  { open: '$', close: '$', display: false },
]

function escaped(source: string, pos: number): boolean {
  let slashes = 0
  while (pos > 0 && source[--pos] === '\\') slashes++
  return slashes % 2 === 1
}

function closingPosition(source: string, close: string, from: number): number {
  let pos = source.indexOf(close, from)
  while (pos >= 0) {
    if (!escaped(source, pos) && (close !== '$' || (
      !/\s/.test(source[pos - 1]) && !/[\d$]/.test(source[pos + 1] || '') && source[pos - 1] !== '$'
    ))) return pos
    pos = source.indexOf(close, pos + close.length)
  }
  return -1
}

/** 在 Markdown 转义和强调解析之前保留 TeX；代码块/行内代码仍由原规则处理。 */
export function markdownMath(md: MarkdownIt): void {
  // 缓存随解析状态释放，不跨段落、文档或 Markdown 容器视图复用。
  const inlineFailures = new WeakMap<object, Map<string, { from: number; until: number }>>()
  const blockFailures = new WeakMap<object, Map<string, { from: number; until: number }>>()
  md.inline.ruler.before('escape', 'math_inline', (state, silent) => {
    const start = state.pos
    const delimiter = delimiters.find(d => state.src.startsWith(d.open, start))
    if (!delimiter) return false
    const from = start + delimiter.open.length
    // 单美元沿用常见数学边界，避免将金额和转义美元识别为公式。
    if (delimiter.open === '$' && (/\s/.test(state.src[from] || '') || /\d/.test(state.src[start - 1] || ''))) return false
    let failures = inlineFailures.get(state)
    if (!failures) inlineFailures.set(state, failures = new Map())
    // 链接解析会临时缩短 posMax；只复用同一范围内已证实无闭合符的后缀。
    const key = `${state.posMax}:${delimiter.close}`
    const failed = failures.get(key)
    if (failed && from >= failed.from && from <= failed.until) return false
    // 行内公式原本就不能跨行；先限定到当前行，避免反复查找后续行的闭合符。
    const newline = delimiter.display ? -1 : state.src.indexOf('\n', from)
    const until = newline < 0 ? state.posMax : Math.min(newline, state.posMax)
    const end = closingPosition(state.src.slice(0, until), delimiter.close, from)
    if (end < 0) {
      failures.set(key, { from, until })
      return false
    }
    if (!state.src.slice(from, end).trim()) return false
    if (!silent) {
      const token = state.push('math_inline', 'math', 0)
      token.content = state.src.slice(from, end)
      token.markup = delimiter.open
      token.meta = { display: delimiter.display, source: state.src.slice(start, end + delimiter.close.length) }
    }
    state.pos = end + delimiter.close.length
    return true
  })

  md.block.ruler.before('fence', 'math_block', (state, startLine, endLine, silent) => {
    if (state.sCount[startLine] - state.blkIndent >= 4) return false
    const first = state.src.slice(state.bMarks[startLine] + state.tShift[startLine], state.eMarks[startLine])
    const delimiter = delimiters.find(d => d.display && first.startsWith(d.open))
    if (!delimiter) return false
    let failures = blockFailures.get(state)
    if (!failures) blockFailures.set(state, failures = new Map())
    // 引用/列表会临时改写行前缀；嵌套层级、缩进、父规则及结束行共同限定视图。
    const key = JSON.stringify([state.level, state.blkIndent, state.parentType, endLine, delimiter.close])
    const failed = failures.get(key)
    if (failed && startLine >= failed.from && startLine < failed.until) return false
    const failBefore = (until: number): false => {
      failures.set(key, { from: startLine, until })
      return false
    }
    // getLines 去掉引用/列表前缀，且不越过当前 Markdown 容器。
    let last = startLine
    for (; last < endLine; last++) {
      if (last > startLine && state.sCount[last] < state.blkIndent && !state.isEmpty(last)) return failBefore(last)
      const line = state.getLines(last, last + 1, state.blkIndent, false).trim()
      const close = closingPosition(line, delimiter.close, last === startLine ? delimiter.open.length : 0)
      if (close < 0) continue
      // 失败行自身可能是下一个公式的开头，不把它加入已排除区间。
      if (line.slice(close + delimiter.close.length).trim()) return failBefore(last)
      break
    }
    if (last === endLine) return failBefore(endLine)
    const source = state.getLines(startLine, last + 1, state.blkIndent, false).trim()
    const content = source.slice(delimiter.open.length, -delimiter.close.length).trim()
    if (!content) return false
    if (silent) return true
    const token = state.push('math_block', 'math', 0)
    token.block = true
    token.map = [startLine, last + 1]
    token.content = content
    token.meta = { display: true, source }
    state.line = last + 1
    return true
  }, { alt: ['paragraph', 'reference', 'blockquote', 'list'] })

  const render: NonNullable<typeof md.renderer.rules.math_inline> = (tokens, idx) => {
    const token = tokens[idx]
    try {
      return katex.renderToString(token.content, {
        displayMode: token.meta.display, throwOnError: true, trust: false,
        strict: 'ignore', maxSize: 20, maxExpand: 1000,
      }) + (token.block ? '\n' : '')
    } catch {
      const fallback = md.utils.escapeHtml(token.meta.source)
      return token.block ? `<p class="math-fallback">${fallback}</p>\n` : fallback
    }
  }
  md.renderer.rules.math_inline = render
  md.renderer.rules.math_block = render
}
