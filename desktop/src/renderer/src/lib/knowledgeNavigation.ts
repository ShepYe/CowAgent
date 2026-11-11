import type { KnowledgeAction, KnowledgeCatalogPage, KnowledgeList } from '../types'

export interface KnowledgeTarget { path: string; title: string; anchor?: string }

export function isInternalDocumentLink(href: string): boolean {
  if (!href || /^[a-z][\w+.-]*:/i.test(href) || href.startsWith('//') || href.startsWith('/')) return false
  if (href.startsWith('#')) return true
  const path = href.split(/[?#]/, 1)[0]
  try { return /\.md$/i.test(decodeURIComponent(path)) } catch { return /\.md$/i.test(path) }
}

export function knowledgeFiles(list: KnowledgeList): KnowledgeTarget[] {
  const result = (list.root_files || []).map(f => ({ path: f.name, title: f.title || f.name }))
  const walk = (dirs: KnowledgeList['tree'], parent = '') => {
    for (const dir of dirs) {
      const path = parent ? `${parent}/${dir.dir}` : dir.dir
      result.push(...dir.files.map(f => ({ path: `${path}/${f.name}`, title: f.title || f.name })))
      walk(dir.children || [], path)
    }
  }
  walk(list.tree || [])
  return result
}

export function knowledgeLinkAddress(href: string, sourcePath: string): { path: string; anchor?: string } | null {
  if (!isInternalDocumentLink(href)) return null
  const hash = href.indexOf('#')
  let anchor: string | undefined, decoded: string
  try {
    anchor = hash >= 0 ? decodeURIComponent(href.slice(hash + 1)) : undefined
    decoded = decodeURIComponent(href.split(/[?#]/, 1)[0]).replace(/\\/g, '/')
  } catch { return null }
  if (!decoded && !sourcePath) return null
  if (decoded.startsWith('/') || /^[a-z][\w+.-]*:/i.test(decoded) || decoded.includes('\0')) return null
  const absolute = decoded.startsWith('knowledge/')
  const segments = absolute ? [] : (sourcePath ? sourcePath.split('/').slice(0, -1) : [])
  if (!absolute && !sourcePath) return null
  for (const segment of (absolute ? decoded.slice('knowledge/'.length) : decoded).split('/')) {
    if (!segment || segment === '.') continue
    if (segment === '..') {
      if (!segments.length) return null
      segments.pop()
    } else segments.push(segment)
  }
  const path = decoded ? segments.join('/') : sourcePath
  return { path, anchor }
}

export function resolveKnowledgeLink(list: KnowledgeList, href: string, sourcePath: string,
  catalog: KnowledgeCatalogPage[] = []): KnowledgeTarget | null {
  const address = knowledgeLinkAddress(href, sourcePath)
  if (!address) return null
  const { path, anchor } = address
  const target = [...knowledgeFiles(legacyKnowledgeTree(list)), ...catalog.filter(p => p.searchable)].find(f => f.path === path)
  return target ? { path, title: target.title, anchor } : null
}

export function pageCategoryAction(page: KnowledgeCatalogPage, category?: string): KnowledgeAction {
  const payload = { page_id: page.page_id, expected_version: page.page_version, expected_category_revision: page.category_revision }
  return category === undefined ? { action: 'reset_page_category', payload }
    : { action: 'set_page_category', payload: { ...payload, category } }
}

export function stripMatchingHeading(content: string, title: string): string {
  const first = content.match(/^\s*#\s+(.+?)\s*(?:\r?\n|$)/)
  return first && first[1].trim().toLocaleLowerCase() === title.trim().toLocaleLowerCase()
    ? content.slice(first[0].length).replace(/^\s*\r?\n/, '') : content
}

/** 只过滤托管物理树，保留未托管目录和根文件的既有读写入口。 */
export function legacyKnowledgeTree(list: KnowledgeList): KnowledgeList {
  return { ...list, tree: list.tree.filter(dir => dir.dir !== 'wiki') }
}

export function groupCatalog(pages: KnowledgeCatalogPage[], search = ''): [string, KnowledgeCatalogPage[]][] {
  const query = search.trim().toLocaleLowerCase()
  const groups = new Map<string, KnowledgeCatalogPage[]>()
  for (const page of pages) {
    if (!page.searchable || (query && !`${page.title} ${page.category}`.toLocaleLowerCase().includes(query))) continue
    const category = page.category || '未分类'
    groups.set(category, [...(groups.get(category) || []), page])
  }
  return [...groups].sort(([a], [b]) => a.localeCompare(b)).map(([name, rows]) =>
    [name, rows.sort((a, b) => a.title.localeCompare(b.title))])
}

export function headingAnchor(text: string): string {
  return text.trim().toLocaleLowerCase().replace(/[^\p{L}\p{N}\p{M}_\s-]/gu, '').replace(/\s+/g, '-')
}

export function scrollToKnowledgeAnchor(root: HTMLElement | null, anchor?: string): boolean {
  if (!root || !anchor) return true
  const wanted = headingAnchor(anchor)
  const target = Array.from(root.querySelectorAll<HTMLElement>('[id], h1, h2, h3, h4, h5, h6'))
    .find(el => el.id === anchor || el.id === wanted || headingAnchor(el.textContent || '') === wanted)
  target?.scrollIntoView({ block: 'start' })
  return !!target
}
