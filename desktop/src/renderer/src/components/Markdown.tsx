import React, { useMemo, useRef, useCallback, useEffect } from 'react'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import 'katex/dist/katex.min.css'
import '../lib/markdownMath.css'
import { markdownMath } from '../lib/markdownMath'
import { t } from '../i18n'
import apiClient from '../api/client'
import type { KnowledgeFigures, KnowledgeFigureItem } from '../types'
import { workspaceHrefOf } from '../lib/fileKind'
import { useWorkspaceStore } from '../store/workspaceStore'
import { useLightboxStore } from './Lightbox'
import { headingAnchor, isInternalDocumentLink } from '../lib/knowledgeNavigation'

/**
 * 与 web console 使用相同 Markdown 基础引擎，桌面端另支持 TeX 公式。
 */

const md: MarkdownIt = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
  typographer: true,
  highlight(str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return hljs.highlight(str, { language: lang }).value
      } catch {
        /* fall through */
      }
    }
    try {
      return hljs.highlightAuto(str).value
    } catch {
      return ''
    }
  },
})

md.use(markdownMath)

const defaultHeadingOpen = md.renderer.rules.heading_open
md.renderer.rules.heading_open = (tokens, idx, options, env, renderer) => {
  if (env?.knowledgeLinks) {
    const text = tokens[idx + 1]?.children?.filter(token => ['text', 'code_inline'].includes(token.type)).map(token => token.content).join('') || tokens[idx + 1]?.content || ''
    const base = headingAnchor(text)
    const counts: Map<string, number> = env.headingCounts || (env.headingCounts = new Map())
    const count = counts.get(base) || 0
    counts.set(base, count + 1)
    tokens[idx].attrSet('id', count ? `${base}-${count}` : base)
  }
  return defaultHeadingOpen ? defaultHeadingOpen(tokens, idx, options, env, renderer) : renderer.renderToken(tokens, idx, options)
}

// CommonMark's flanking rules treat every Unicode punctuation alike, so
// `是**"引号"**——` never opens emphasis: the quote after `**` is punctuation
// while 是 before it is neither punctuation nor space, and the run degrades to
// literal asterisks. Apply the CJK-friendly amendment
// (github.com/tats-u/markdown-cjk-friendly): a `*` run with a CJK neighbour and
// no adjacent whitespace both opens and closes. `_` keeps the stock rules,
// whose intraword behavior depends on the original classification.
const _CJK_CHAR =
  /[\u1100-\u11FF\u2E80-\u303F\u3040-\u33FF\u3400-\u4DBF\u4E00-\u9FFF\uA960-\uA97F\uAC00-\uD7FF\uF900-\uFAFF\uFE10-\uFE19\uFE30-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6]/
const _StateInline = md.inline.State as unknown as {
  prototype: {
    scanDelims(start: number, canSplitWord: boolean): { can_open: boolean; can_close: boolean; length: number }
    src: string
    posMax: number
  }
}
const _scanDelims = _StateInline.prototype.scanDelims
_StateInline.prototype.scanDelims = function (start, canSplitWord) {
  const res = _scanDelims.call(this, start, canSplitWord)
  if (!canSplitWord) return res
  const lastCode = start > 0 ? this.src.charCodeAt(start - 1) : 0x20
  const nextPos = start + res.length
  const nextCode = nextPos < this.posMax ? this.src.charCodeAt(nextPos) : 0x20
  if (md.utils.isWhiteSpace(lastCode) || md.utils.isWhiteSpace(nextCode)) return res
  if (!_CJK_CHAR.test(String.fromCharCode(lastCode)) && !_CJK_CHAR.test(String.fromCharCode(nextCode))) return res
  res.can_open = true
  res.can_close = true
  return res
}

// Fix greedy linkify: markdown-it's linkify swallows markdown emphasis (`*`)
// and CJK full-width punctuation glued to a URL (common in LLM output like
// `**https://x**，中文`), turning the whole tail into one broken link. Cut the
// URL at the first such char and spill the remainder back as plain text.
const _GREEDY_LINK_CUT = /[*\u3000-\u303F\uFF00-\uFFEF]/
md.core.ruler.after('linkify', 'fix_greedy_linkify', (state) => {
  for (const blk of state.tokens) {
    if (blk.type !== 'inline' || !blk.children) continue
    const ch = blk.children
    for (let i = 0; i < ch.length; i++) {
      const open = ch[i]
      if (open.type !== 'link_open' || open.markup !== 'linkify') continue
      const textTok = ch[i + 1]
      const close = ch[i + 2]
      if (!textTok || textTok.type !== 'text' || !close || close.type !== 'link_close') continue
      const idx = textTok.content.search(_GREEDY_LINK_CUT)
      if (idx < 0) continue
      const keep = textTok.content.slice(0, idx)
      const spill = textTok.content.slice(idx)
      textTok.content = keep
      open.attrSet('href', keep)
      const spillTok = new state.Token('text', '', 0)
      spillTok.content = spill
      ch.splice(i + 3, 0, spillTok)
    }
  }
})

// Open links in a new tab safely.
const defaultLinkOpen =
  md.renderer.rules.link_open ||
  function (tokens, idx, options, _env, self) {
    return self.renderToken(tokens, idx, options)
  }
md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
  const token = tokens[idx]
  // A workspace-relative href means nothing outside the app: `target=_blank`
  // would hand it to the OS browser, which resolves it against the renderer's
  // file:// URL. Tag it so the click handler opens the preview panel instead.
  const wsPath = workspaceHrefOf(token.attrGet('href') || '')
  if (wsPath) {
    token.attrPush(['data-ws-path', wsPath])
    token.attrJoin('class', 'ws-link')
  } else {
    token.attrPush(['target', '_blank'])
    token.attrPush(['rel', 'noopener noreferrer'])
  }
  return defaultLinkOpen(tokens, idx, options, env, self)
}

// An image the agent produced is referenced by its local path (`/Users/.../x.png`
// or `~/...`). The renderer runs from file:// or the dev server, so such a src
// never resolves on its own — route it through the backend file endpoint, which
// takes an absolute path and enforces the allowed roots.
const defaultImage =
  md.renderer.rules.image ||
  function (tokens, idx, options, _env, self) {
    return self.renderToken(tokens, idx, options)
  }
md.renderer.rules.image = function (tokens, idx, options, env, self) {
  const token = tokens[idx]
  const src = token.attrGet('src') || ''
  if (src.startsWith('knowledge-figure:')) {
    const match = /^knowledge-figure:([A-Za-z0-9_-]{1,128})\/([A-Za-z0-9_-]{1,128})$/.exec(src)
    if (!match) return '<span class="knowledge-figure-state">图片不可用</span>'
    const figures = env?.figures as KnowledgeFigures | undefined
    if (figures?.publish_id && figures.publish_id !== match[1]) return '<span class="knowledge-figure-state">图片不可用</span>'
    const item = figures?.figure_manifest?.items.find(i => i.figure_id === match[2])
    const caption = item?.caption || token.content
    const original = item ? figureOriginal(item, match[1], env.figureView, env.agentId) : ''
    return `<span class="knowledge-figure" data-publish-id="${match[1]}" data-figure-id="${match[2]}"><span class="knowledge-figure-state">加载图片…</span><span class="knowledge-figure-caption">${md.utils.escapeHtml(caption)} ${original} <span title="已核验的原图或裁图；点击图片放大，原件入口可核对出处。">?</span></span></span>`
  }
  const baseDir = (env as { imageBaseDir?: string } | undefined)?.imageBaseDir
  if (/^\/(?:api\/file|preview)\b/.test(src)) {
    // The backend already rewrote a workspace-relative ref to a site-absolute
    // URL (/api/file?path=... or /preview/...). The renderer isn't same-origin
    // with the backend, so prefix it with the backend base (and carry the auth
    // token) rather than treating it as a filesystem path to re-wrap.
    token.attrSet('src', apiClient.getFileUrl(src))
  } else if (/^~\//.test(src) || src.startsWith('/')) {
    token.attrSet('src', apiClient.getServeFileUrl(src))
  } else if (baseDir && !/^[a-zA-Z][\w+.-]*:/.test(src)) {
    // Doc-relative image (e.g. knowledge markdown `../images/x.png`): resolve
    // against the doc's directory (passed via render env) and serve via /api/file.
    let rel = src
    try {
      rel = decodeURIComponent(rel)
    } catch {
      /* keep the raw form */
    }
    const combined = `${baseDir}/${rel.split('?')[0]}`
    const segments: string[] = []
    for (const seg of combined.split('/')) {
      if (seg === '..') segments.pop()
      else if (seg !== '.' && seg !== '') segments.push(seg)
    }
    // Unix baseDir is absolute, so restore the leading slash split() dropped
    // (a Windows drive path like C:/x keeps its own prefix). /api/file rejects
    // non-absolute paths.
    const resolved = (combined.startsWith('/') ? '/' : '') + segments.join('/')
    token.attrSet('src', apiClient.getServeFileUrl(resolved))
  }
  return defaultImage(tokens, idx, options, env, self)
}

// A table can't shrink below its columns' minimum content width, so a wide
// comparison table would run past the bubble. Wrap it in a scroller: the table
// keeps filling the bubble when it fits and scrolls sideways when it doesn't.
const defaultTableOpen =
  md.renderer.rules.table_open ||
  function (tokens, idx, options, _env, self) {
    return self.renderToken(tokens, idx, options)
  }
const defaultTableClose =
  md.renderer.rules.table_close ||
  function (tokens, idx, options, _env, self) {
    return self.renderToken(tokens, idx, options)
  }
md.renderer.rules.table_open = function (tokens, idx, options, env, self) {
  return `<div class="table-wrap">` + defaultTableOpen(tokens, idx, options, env, self)
}
md.renderer.rules.table_close = function (tokens, idx, options, env, self) {
  return defaultTableClose(tokens, idx, options, env, self) + `</div>`
}

// Wrap fenced code blocks so we can render a header (lang + copy button).
const defaultFence =
  md.renderer.rules.fence ||
  function (tokens, idx, options, _env, self) {
    return self.renderToken(tokens, idx, options)
  }
md.renderer.rules.fence = function (tokens, idx, options, env, self) {
  const token = tokens[idx]
  const info = token.info ? token.info.trim().split(/\s+/)[0] : ''
  // Ensure the `hljs` class is present so the GitHub theme background/base
  // color applies (markdown-it only adds language-* by default).
  let rendered = defaultFence(tokens, idx, options, env, self)
  if (rendered.includes('<code class="')) {
    rendered = rendered.replace('<code class="', '<code class="hljs ')
  } else {
    rendered = rendered.replace('<code>', '<code class="hljs">')
  }
  return (
    `<div class="code-block-wrapper">` +
    `<div class="code-block-header">` +
    `<span class="code-block-lang">${info || 'text'}</span>` +
    `<button type="button" class="code-copy-btn" data-code-id="cb-${idx}" aria-label="Copy code">${t('msg_copy')}</button>` +
    `</div>` +
    rendered +
    `</div>`
  )
}

/** The HTML the renderer would show, for a picture of the text rather than a live view. */
export function markdownToHtml(content: string): string {
  return md.render(content || '')
}

function figureOriginal(item: KnowledgeFigureItem, publish: string, view: 'current' | 'history', agentId?: string) {
  const location = item.location || {}, page = location.physical_page
  const slide = location.source_slide_number
  const office = location.kind === 'slide' || location.kind === 'word_reading_copy_page'
  const url = apiClient.getKnowledgeFigureOriginalUrl(publish, item.figure_key, view, agentId)
  const label = slide ? `原件 · 幻灯片${slide}` : page ? `原件 · 第${page}页` : '原件'
  let html = `<a href="${md.utils.escapeHtml(url + (page && !office ? `#page=${page}` : ''))}" target="_blank" rel="noopener noreferrer">${label} ↗</a>`
  if (office) {
    const copy = apiClient.getKnowledgeFigureOriginalUrl(publish, item.figure_key, view, agentId, 'reading-copy')
    html += ` <a href="${md.utils.escapeHtml(copy)}#page=${item.source_page || location.reading_copy_page || slide || 1}" target="_blank" rel="noopener noreferrer" title="PDF 阅读副本的页码；与原件位置的对应关系由阅读时保存的映射确定。">阅读副本 · 第${item.source_page || location.reading_copy_page || slide || 1}页 ↗</a>`
  }
  return html
}

const figureQueue: (() => Promise<void>)[] = []
let figureRequests = 0
function queueFigure(job: () => Promise<void>) {
  figureQueue.push(job)
  const drain = () => {
    while (figureRequests < 4 && figureQueue.length) {
      figureRequests++
      figureQueue.shift()!().finally(() => { figureRequests--; drain() })
    }
  }
  drain()
}

interface MarkdownProps {
  content: string
  /**
   * Intercept clicks on internal document links (relative `.md` hrefs). When
   * provided, such links open in-app instead of being handed to the OS. Used by
   * the knowledge viewer so index links open the target doc rather than firing
   * an "application cannot be opened (-120)" error in Electron.
   */
  onInternalLink?: (href: string) => void
  /**
   * Absolute directory of the document the markdown comes from, so image srcs
   * that are relative to it (`../images/x.png`) resolve to real files. Used by
   * the knowledge viewer; without it relative srcs are left untouched.
   */
  imageBaseDir?: string
  figures?: KnowledgeFigures
  figureView?: 'current' | 'history'
  agentId?: string
}

const Markdown: React.FC<MarkdownProps> = ({ content, onInternalLink, imageBaseDir, figures, figureView = 'current', agentId }) => {
  const rootRef = useRef<HTMLDivElement>(null)

  const controlled = !!figures?.figure_manifest || content.includes('knowledge-figure:')
  const backend = controlled ? apiClient.getBaseUrl() : ''
  const scope = agentId ?? (controlled ? apiClient.getActiveAgentId() : '')
  const identity = `${backend}:${scope}:${figures?.publish_id || ''}:${figureView}`
  const html = useMemo(() => {
    let rendered = md.render(content || '', { imageBaseDir, knowledgeLinks: !!onInternalLink, figures, figureView, agentId: scope })
    for (const item of figures?.figure_manifest?.items || []) {
      if (item.state !== 'pending') continue
      rendered += `<p class="knowledge-figure-state">待补图 ${figureOriginal(item, figures!.publish_id!, figureView, scope)} <span title="正文已核验；图片尚未生成，补图将发布为新版本。">?</span></p>`
    }
    return rendered
  }, [content, imageBaseDir, onInternalLink, figures, identity])

  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const controller = new AbortController(), urls: string[] = []
    let active = true
    const valid = () => active && backend === apiClient.getBaseUrl() && scope === (agentId ?? apiClient.getActiveAgentId())
    const load = (element: HTMLElement) => {
      if (element.dataset.loading) return
      element.dataset.loading = 'true'
      queueFigure(async () => {
        if (!valid()) return
        try {
          const response = await fetch(apiClient.getKnowledgeFigureUrl(element.dataset.publishId!, element.dataset.figureId!, figureView, scope), { signal: controller.signal, cache: 'no-store' })
          if (!response.ok || !response.headers.get('Content-Type')?.startsWith('image/png')) throw new Error('unavailable')
          const blob = await response.blob()
          if (!valid()) return
          const url = URL.createObjectURL(blob); urls.push(url)
          const img = document.createElement('img')
          img.alt = element.querySelector('.knowledge-figure-caption')?.textContent || ''
          img.loading = 'lazy'; img.src = url
          img.onerror = () => { if (valid()) { img.remove(); element.querySelector('.knowledge-figure-state')!.textContent = '图片不可用' } }
          img.onload = () => { if (valid()) element.querySelector('.knowledge-figure-state')!.textContent = '' }
          element.prepend(img)
        } catch { if (valid()) element.querySelector('.knowledge-figure-state')!.textContent = '图片不可用' }
      })
    }
    const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting) { observer!.unobserve(entry.target); load(entry.target as HTMLElement) }
    }, { rootMargin: '200px' })
    root.querySelectorAll<HTMLElement>('.knowledge-figure[data-figure-id]').forEach(element => observer ? observer.observe(element) : load(element))
    return () => {
      active = false
      controller.abort()
      observer?.disconnect()
      const lightbox = useLightboxStore.getState()
      if (lightbox.src && urls.includes(lightbox.src)) lightbox.close()
      urls.forEach(URL.revokeObjectURL)
    }
  }, [html, identity])

  // Delegate clicks: images zoom, copy buttons on code blocks, internal doc links.
  const handleClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const target = e.target as HTMLElement

      // Any inline image opens in the lightbox.
      const img = target.closest('img') as HTMLImageElement | null
      if (img) {
        useLightboxStore.getState().open(img.currentSrc || img.src)
        return
      }

      // Internal knowledge links (relative *.md), when a handler is provided.
      if (onInternalLink) {
        const a = target.closest('a') as HTMLAnchorElement | null
        if (a) {
          const href = a.getAttribute('href') || ''
          if (isInternalDocumentLink(href)) {
            e.preventDefault()
            onInternalLink(href)
            return
          }
        }
      }

      // Links to workspace files open in the preview panel.
      const wsLink = target.closest('a[data-ws-path]') as HTMLAnchorElement | null
      if (wsLink) {
        e.preventDefault()
        useWorkspaceStore.getState().openLink(wsLink.dataset.wsPath || '')
        return
      }

      const btn = target.closest('.code-copy-btn') as HTMLElement | null
      if (!btn) return
      const pre = btn.closest('.code-block-wrapper')?.querySelector('pre')
      if (!pre) return
      navigator.clipboard.writeText(pre.textContent || '')
      const original = btn.textContent
      btn.textContent = t('msg_copied')
      btn.classList.add('copied')
      setTimeout(() => {
        btn.textContent = original
        btn.classList.remove('copied')
      }, 1600)
    },
    [onInternalLink]
  )

  return (
    <div
      key={identity}
      ref={rootRef}
      className="msg-content text-sm text-content leading-relaxed break-words"
      onClick={handleClick}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}

export default Markdown
