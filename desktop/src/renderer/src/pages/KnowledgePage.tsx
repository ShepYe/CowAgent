import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Loader2,
  Search,
  FileText,
  ChevronRight,
  ChevronDown,
  MessageSquarePlus,
  Network,
  Files,
  Plus,
  FolderPlus,
  FilePlus2,
  Upload,
  ArrowRightLeft,
  Pencil,
  Trash2,
  ArrowLeft,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { t, tf, getLang } from '../i18n'
import apiClient from '../api/client'
import type {
  KnowledgeAction,
  KnowledgeDir,
  KnowledgeFile,
  KnowledgeList,
  KnowledgeReadResult,
  KnowledgeCatalog,
  KnowledgeCatalogPage,
  KnowledgeGraph as KnowledgeGraphData,
} from '../types'
import Markdown from '../components/Markdown'
import KnowledgeGraph from '../components/KnowledgeGraph'
import PersonalTasksPanel from './PersonalTasksPanel'
import { DocActions, DocEditor, DocNotice, DocView } from '../components/DocEditor'
import { createDocEditorStore } from '../store/docEditorStore'
import { askConfirm } from '../store/confirmStore'
import AgentScopeSelect from '../components/AgentScopeSelect'
import Tooltip from '../components/Tooltip'
import { useAgentStore, selectMultiAgent } from '../store/agentStore'
import KnowledgeCatalogView, { KnowledgeCatalogGroups } from '../components/KnowledgeCatalog'
import KnowledgeHistory from '../components/KnowledgeHistory'
import KnowledgeVersionView from '../components/KnowledgeVersionView'
import KnowledgeCategoryMenu from '../components/KnowledgeCategoryMenu'
import { knowledgeLinkAddress, legacyKnowledgeTree, pageCategoryAction, resolveKnowledgeLink, scrollToKnowledgeAnchor, stripMatchingHeading } from '../lib/knowledgeNavigation'

interface KnowledgePageProps {
  baseUrl: string
}

type Tab = 'docs' | 'graph' | 'personal'

const KNOWLEDGE_IMPORT_MAX_FILES = 100
const KNOWLEDGE_IMPORT_MAX_FILE_SIZE = 10 * 1024 * 1024
const KNOWLEDGE_IMPORT_MAX_TOTAL_SIZE = 200 * 1024 * 1024

// Pages the Agent maintains: index.md is regenerated from the tree and log.md
// is its own journal. The backend refuses both; this is only so the viewer can
// say why instead of falling back to the generic "unsupported file" reason.
const PROTECTED_KNOWLEDGE_PAGES = ['index.md', 'log.md']

/** A knowledge page, as the editor addresses it. */
interface KnowledgeRef {
  path: string
  /** Title to show until the tree is re-read; see `docTitle` below. */
  title: string
  /** Whose base the page belongs to. Empty in single-Agent mode. */
  agentId: string
  /**
   * Absolute directory of the page, for resolving image srcs written relative
   * to it. Filled in by the read rather than by the caller, since only the
   * response knows where the base resolved to; the render that draws the new
   * content picks it up, so it needs no state of its own.
   */
  dir?: string
}

/**
 * Created at module scope so an unsaved edit survives this page being unmounted
 * by a route change, which is also what lets the navigation guard find it.
 *
 * Saving goes through the knowledge endpoint rather than the workspace one: it
 * reindexes the page afterwards, so semantic search stops answering with the
 * pre-edit text.
 */
const knowledgeEditor = createDocEditorStore<KnowledgeRef, KnowledgeReadResult>({
  keyOf: (doc) => `${doc.agentId}:${doc.path}`,
  read: async (doc) => {
    const res = await apiClient.readKnowledge(doc.path, doc.agentId || undefined)
    doc.dir = res.dir || ''
    return res
  },
  write: async (doc, content, expectedMtime) => {
    const res = await apiClient.knowledgeAction({
      action: 'update_document',
      payload: { path: doc.path, content, expected_mtime: expectedMtime },
      ...(doc.agentId ? { agent_id: doc.agentId } : {}),
    })
    // The knowledge endpoint answers in its own envelope: `code` there is an
    // HTTP status, and a conflict is flagged inside the payload instead.
    const payload = (res.payload || {}) as { conflict?: boolean; mtime?: number }
    return {
      status: res.status,
      message: typeof res.message === 'string' ? res.message : undefined,
      code: payload.conflict ? 'conflict' : undefined,
      mtime: payload.mtime,
    }
  },
  // The read never truncates, so size is the only other reason it reports a
  // page as uneditable - the bound the save would reject it against anyway.
  refusal: (data) =>
    isProtectedPage(data.path) ? t('knowledge_doc_readonly') : t('ws_edit_too_large'),
})

const formatSize = (bytes: number): string => {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
}

// The viewer already shows the doc title above the body, so a leading `# H1`
// that repeats it looks duplicated. Drop that first H1 (and any blank lines
// right after it) when it matches the title; leave the body untouched otherwise.
function stripDuplicateH1(content: string, title: string): string {
  return stripMatchingHeading(content, title)
}

// Flatten the tree into category paths (for destination selectors).
function categoryPaths(dirs: KnowledgeDir[], parent = ''): string[] {
  const paths: string[] = []
  for (const dir of dirs || []) {
    if (!parent && ['wiki', 'managed'].includes(dir.dir)) continue
    const path = parent ? `${parent}/${dir.dir}` : dir.dir
    paths.push(path, ...categoryPaths(dir.children || [], path))
  }
  return paths
}

// Validate a batch of files chosen for import. Returns an error message or ''.
function validateImportFiles(files: File[]): string {
  if (!files.length) return t('knowledge_import_choose_files')
  if (files.length > KNOWLEDGE_IMPORT_MAX_FILES) {
    return tf('knowledge_import_too_many', { max: KNOWLEDGE_IMPORT_MAX_FILES })
  }
  let total = 0
  for (const file of files) {
    total += file.size || 0
    if ((file.size || 0) > KNOWLEDGE_IMPORT_MAX_FILE_SIZE) {
      return tf('knowledge_import_file_too_large', { name: file.name })
    }
  }
  if (total > KNOWLEDGE_IMPORT_MAX_TOTAL_SIZE) return t('knowledge_import_total_too_large')
  return ''
}

// ---- Dialog model ----------------------------------------------------------

interface DialogState {
  kind: 'category' | 'doc-pick-category' | 'document' | 'import' | 'rename-category' | 'move-document'
  category?: string
  files?: File[]
  /** The category being renamed, or the document being moved. */
  path?: string
}

// Index pages the Agent maintains; the backend refuses to move or delete them.
const isProtectedPage = (path: string) => PROTECTED_KNOWLEDGE_PAGES.includes(path) || /^(wiki|managed)\//.test(path)

const displayCategory = (path: string) => PROTECTED_KNOWLEDGE_PAGES.includes(path) ? '' : path.startsWith('managed/') ? '资料原文' : path.startsWith('wiki/') ? '' : path.split('/').slice(0, -1).join(' / ') || '未分类'

// Where a document can be moved to: any category but the one it is already in.
const moveTargets = (categories: string[], path: string) => {
  const current = path.split('/').slice(0, -1).join('/')
  return categories.filter((c) => c !== current)
}

// Find the first document (root files first, then a DFS over the tree).
function firstFile(list: KnowledgeList): { path: string; title: string } | null {
  const root = list.root_files?.[0]
  if (root) return { path: root.name, title: root.title || root.name }
  const walk = (dir: KnowledgeDir, prefix: string): { path: string; title: string } | null => {
    const dirPath = prefix ? `${prefix}/${dir.dir}` : dir.dir
    const f = dir.files[0]
    if (f) return { path: `${dirPath}/${f.name}`, title: f.title || f.name }
    for (const c of dir.children) {
      const hit = walk(c, dirPath)
      if (hit) return hit
    }
    return null
  }
  for (const d of list.tree || []) {
    const hit = walk(d, '')
    if (hit) return hit
  }
  return null
}

// Resolve a document's display title from its path, falling back to the stem.
function findTitle(list: KnowledgeList, path: string, knownTitle?: string): string {
  const fallback = knownTitle || (path.startsWith('managed/') ? '资料原文' : path.startsWith('wiki/') ? '知识页' : path.split('/').pop()?.replace(/\.md$/i, '') || path)
  for (const f of list.root_files || []) {
    if (f.name === path) return f.title || fallback
  }
  const walk = (dir: KnowledgeDir, prefix: string): string | null => {
    const dirPath = prefix ? `${prefix}/${dir.dir}` : dir.dir
    for (const f of dir.files) {
      if (`${dirPath}/${f.name}` === path) return f.title || fallback
    }
    for (const c of dir.children) {
      const hit = walk(c, dirPath)
      if (hit) return hit
    }
    return null
  }
  for (const d of list.tree || []) {
    const hit = walk(d, '')
    if (hit) return hit
  }
  return fallback
}

const KnowledgePage: React.FC<KnowledgePageProps> = ({ baseUrl }) => {
  const navigate = useNavigate()
  const [tab, setTab] = useState<Tab>('docs')
  const [data, setData] = useState<KnowledgeList | null>(null)
  const [catalog, setCatalog] = useState<KnowledgeCatalog | null>(null)
  const [catalogError, setCatalogError] = useState('')
  const [knowledgeRevision, setKnowledgeRevision] = useState(0)
  const [historyPublishId, setHistoryPublishId] = useState<string | null>(null)
  const [pendingAnchor, setPendingAnchor] = useState<{ path: string; anchor: string } | null>(null)
  const viewerRef = useRef<HTMLDivElement>(null)
  const loadSequence = useRef(0)
  const loadedScope = useRef<string | null>(null)
  const navigationSequence = useRef(0)
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')

  // Which Agent's knowledge base is shown. The first visit shows the default
  // Agent; the last choice is remembered across visits (and dropped if that
  // Agent no longer exists). Empty in single-Agent mode (legacy path).
  const multiAgent = useAgentStore(selectMultiAgent)
  const defaultAgentId = useAgentStore((s) => s.defaultAgentId)
  const agents = useAgentStore((s) => s.agents)
  const [scopeAgentId, setScopeAgentId] = useState<string>(
    () => localStorage.getItem('cow_knowledge_agent') || ''
  )
  const remembered = agents.some((a) => a.id === scopeAgentId) ? scopeAgentId : ''
  const viewingAgentId = multiAgent ? remembered || defaultAgentId : ''

  // The open document lives in the editor store, so an unsaved edit is not tied
  // to this component being mounted.
  const doc = knowledgeEditor((s) => s.doc)
  const content = knowledgeEditor((s) => s.content)
  const docLoading = knowledgeEditor((s) => s.loading)
  const edit = knowledgeEditor((s) => s.edit)
  const readonly = knowledgeEditor((s) => s.readonly)
  const editorRef = useRef<HTMLTextAreaElement>(null)
  const activePath = doc?.path ?? null
  // Taken from the tree rather than from whatever opened the page, so a save
  // that rewrote the H1 retitles the header as soon as the tree is re-read.
  const currentPage = catalog?.pages.find(page => page.path === activePath && page.searchable)
  const docTitle = currentPage?.title || (doc ? (data ? findTitle(data, doc.path, doc.title) : doc.title) : '')
  const legacyTree = useMemo(() => data ? legacyKnowledgeTree(data) : null, [data])

  const [graph, setGraph] = useState<KnowledgeGraphData | null>(null)
  const [graphLoading, setGraphLoading] = useState(false)
  const graphSequence = useRef(0)
  const invalidateGraph = useCallback(() => {
    graphSequence.current++
    setGraph(null)
    setGraphLoading(false)
  }, [])

  // Management UI state.
  const [menuOpen, setMenuOpen] = useState(false)
  const [dialog, setDialog] = useState<DialogState | null>(null)
  const [status, setStatus] = useState<{ text: string; error: boolean } | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const statusTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const showStatus = useCallback((text: string, error = false, sticky = false) => {
    if (statusTimer.current) clearTimeout(statusTimer.current)
    setStatus({ text, error })
    if (!sticky) {
      statusTimer.current = setTimeout(() => setStatus(null), 4000)
    }
  }, [])

  const openDoc = useCallback(
    (path: string, title: string) => {
      navigationSequence.current++
      setHistoryPublishId(null)
      setPendingAnchor(null)
      void knowledgeEditor.getState().open({ path, title, agentId: viewingAgentId })
    },
    [viewingAgentId]
  )

  const openInternalLink = useCallback(
    async (href: string, sourcePath = activePath || '') => {
      if (!data) { showStatus('目录尚未就绪，请稍后重试', true); return }
      if (href.startsWith('#')) {
        try {
          if (!scrollToKnowledgeAnchor(viewerRef.current, decodeURIComponent(href.slice(1)))) showStatus('未找到该文档位置', true)
        } catch { showStatus('链接编码无效', true) }
        return
      }
      const hit = resolveKnowledgeLink(data, href, sourcePath, catalog?.pages)
      if (!hit) {
        const address = knowledgeLinkAddress(href, sourcePath)
        if (!address?.path.startsWith('wiki/')) { showStatus('未找到对应知识页或链接位置无效', true); return }
        const mine = ++navigationSequence.current
        try {
          const result = await apiClient.getKnowledgeVersionByPath(address.path, viewingAgentId || undefined)
          if (mine !== navigationSequence.current) return
          if (result.status !== 'success') throw new Error(result.message || '未找到对应历史发布')
          if (!(await knowledgeEditor.getState().guard()) || mine !== navigationSequence.current) return
          setHistoryPublishId(result.version.publish_id)
          if (address.anchor) setPendingAnchor({ path: result.version.path || address.path, anchor: address.anchor })
        } catch (e) { if (mine === navigationSequence.current) showStatus(e instanceof Error ? e.message : '历史链接读取失败', true) }
        return
      }
      openDoc(hit.path, hit.title)
      if (hit.anchor) setPendingAnchor({ path: hit.path, anchor: hit.anchor })
    },
    [data, catalog, activePath, openDoc, showStatus, viewingAgentId]
  )

  const returnToDirectory = async () => {
    const mine = ++navigationSequence.current
    setPendingAnchor(null)
    if (await knowledgeEditor.getState().close() && mine === navigationSequence.current) setHistoryPublishId(null)
  }

  useEffect(() => {
    if (docLoading || !pendingAnchor || pendingAnchor.path !== activePath || historyPublishId) return
    const timer = setTimeout(() => {
      if (!scrollToKnowledgeAnchor(viewerRef.current, pendingAnchor.anchor)) showStatus('已打开知识页，未找到指定位置', true)
      setPendingAnchor(null)
    }, 0)
    return () => clearTimeout(timer)
  }, [docLoading, activePath, content, pendingAnchor, historyPublishId, showStatus])

  const loadKnowledge = useCallback(async () => {
    const sequence = ++loadSequence.current
    const [fresh, result] = await Promise.all([
      apiClient.getKnowledgeList(viewingAgentId || undefined),
      apiClient.getKnowledgeCatalog(viewingAgentId || undefined).catch(() => null),
    ])
    if (sequence !== loadSequence.current) return null
    loadedScope.current = `${baseUrl}:${viewingAgentId}`
    setData(fresh)
    setCatalog(result?.status === 'success' ? result : null)
    setCatalogError(result?.status === 'success' ? '' : result?.message || '当前目录读取失败，请刷新重试')
    setKnowledgeRevision(v => v + 1)
    invalidateGraph()
    return fresh
  }, [baseUrl, viewingAgentId, invalidateGraph])

  // Reload the tree. When targetPath is given, open it; null means the open doc
  // no longer exists, so fall back to the first one; otherwise keep the
  // currently open doc (or open the first one on the initial load).
  const refresh = useCallback(
    async (targetPath?: string | null) => {
      try {
        const fresh = await loadKnowledge()
        if (!fresh) return null
        if (targetPath) {
          openDoc(targetPath, findTitle(fresh, targetPath))
        } else if (targetPath === null || !activePath) {
          const first = firstFile(fresh)
          if (first) openDoc(first.path, first.title)
        }
        return fresh
      } catch (e) {
        showStatus('知识目录读取失败，请重试', true)
        return null
      }
    },
    [openDoc, activePath, loadKnowledge, showStatus]
  )

  useEffect(() => {
    apiClient.setBaseUrl(baseUrl)
    let cancelled = false
    ;(async () => {
      setLoading(true)
      // Switching the viewed Agent shows a different base, so drop the open doc
      // and its graph and reopen the first file of the new base. Anything
      // unsaved was already asked about by setScope, which is the only way in.
      knowledgeEditor.getState().forget()
      navigationSequence.current++
      setHistoryPublishId(null)
      setCatalog(null)
      invalidateGraph()
      try {
        const fresh = await loadKnowledge()
        if (cancelled || !fresh) return
        const first = firstFile(fresh)
        if (first) openDoc(first.path, first.title)
      } catch (e) {
        if (!cancelled) showStatus('知识目录读取失败，请重试', true)
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
      loadSequence.current++
      navigationSequence.current++
      graphSequence.current++
    }
    // Reload on base URL or viewed-Agent change; refresh() handles in-page edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, viewingAgentId])

  const setScope = async (id: string) => {
    // Another Agent's base replaces the open document, edit and all.
    if (!(await knowledgeEditor.getState().guard())) return
    navigationSequence.current++
    loadSequence.current++
    invalidateGraph()
    setScopeAgentId(id)
    localStorage.setItem('cow_knowledge_agent', id)
  }

  const changePageCategory = async (page: KnowledgeCatalogPage, category?: string) => {
    const action = pageCategoryAction(page, category)
    const result = await apiClient.knowledgeAction({ ...action, ...(viewingAgentId ? { agent_id: viewingAgentId } : {}) })
    if (result.status !== 'success') {
      const message = typeof result.message === 'string' ? result.message : '分类已变化，请核对后重试'
      showStatus(message, true, true)
      await refresh()
      throw new Error(message)
    }
    await refresh()
    showStatus(category === undefined ? '已恢复自动分类' : '分类已保存')
  }

  // A save can rewrite the page's H1 - its title in the tree - and always moves
  // its size, so the tree has to be re-read for the header and the stats line.
  useEffect(
    () =>
      knowledgeEditor.subscribe((state, prev) => {
        // Same document, new text, with an editor open: only a save does that.
        if (prev.edit && state.doc === prev.doc && state.content !== prev.content) void refresh()
      }),
    [refresh]
  )

  const loadGraph = useCallback(async () => {
    if (graph || loadedScope.current !== `${baseUrl}:${viewingAgentId}`) return
    const mine = ++graphSequence.current
    setGraphLoading(true)
    try {
      const result = await apiClient.getKnowledgeGraph(viewingAgentId || undefined)
      if (mine === graphSequence.current) setGraph(result)
    } catch (e) {
      if (mine !== graphSequence.current) return
      console.error('Failed to load graph:', e)
      setGraph({ nodes: [], links: [] })
    } finally {
      if (mine === graphSequence.current) setGraphLoading(false)
    }
  }, [baseUrl, graph, viewingAgentId])

  useEffect(() => { if (tab === 'graph' && !loading && !graph && !graphLoading) void loadGraph() }, [tab, loading, knowledgeRevision, graph, graphLoading, loadGraph])

  const onPersonalKnowledgeChanged = useCallback(() => {
    // 任务发布版本或有效性变化后刷新统计，并让图谱下次打开时读取新数据。
    invalidateGraph()
    void refresh()
  }, [refresh, invalidateGraph])

  const switchTab = async (next: Tab) => {
    if (next === tab) return
    // The graph tab hides the documents panel, text area and all.
    if (!(await knowledgeEditor.getState().guard())) return
    setTab(next)
  }

  // Jump from a graph node to its document.
  const onGraphSelect = useCallback(
    (id: string, label: string) => {
      setTab('docs')
      openDoc(id, label)
    },
    [openDoc]
  )

  // ---- Management actions --------------------------------------------------

  const categories = useMemo(() => categoryPaths(data?.tree || []), [data])

  const createCategory = useCallback(
    async (path: string): Promise<string | null> => {
      showStatus(t('knowledge_working'), false, true)
      try {
        const res = await apiClient.knowledgeAction({
          action: 'create_category',
          payload: { path },
          ...(viewingAgentId ? { agent_id: viewingAgentId } : {}),
        })
        if (res.status !== 'success') {
          showStatus((res.message as string) || t('knowledge_request_failed'), true)
          return null
        }
        showStatus(t('knowledge_category_created'))
        await refresh()
        return path
      } catch {
        showStatus(t('knowledge_request_failed'), true)
        return null
      }
    },
    [refresh, showStatus, viewingAgentId]
  )

  const createDocument = useCallback(
    async (path: string, content: string): Promise<string | null> => {
      showStatus(t('knowledge_working'), false, true)
      try {
        const res = await apiClient.knowledgeAction({
          action: 'create_document',
          payload: { path, content, overwrite: false },
          ...(viewingAgentId ? { agent_id: viewingAgentId } : {}),
        })
        if (res.status !== 'success') {
          showStatus((res.message as string) || t('knowledge_request_failed'), true)
          return null
        }
        const created = ((res.payload as { path?: string })?.path) || path
        showStatus(t('knowledge_document_created'))
        await refresh(created)
        return created
      } catch {
        showStatus(t('knowledge_request_failed'), true)
        return null
      }
    },
    [refresh, showStatus, viewingAgentId]
  )

  const importDocuments = useCallback(
    async (files: File[], targetCategory: string): Promise<boolean> => {
      const err = validateImportFiles(files)
      if (err) {
        showStatus(err, true)
        return false
      }
      const supported = files.filter((f) => /\.(md|txt)$/i.test(f.name || ''))
      if (!supported.length) {
        showStatus(t('knowledge_import_choose_files'), true)
        return false
      }
      showStatus(t('knowledge_importing'), false, true)
      try {
        const res = await apiClient.importKnowledge(supported, targetCategory, viewingAgentId || undefined)
        if (res.status !== 'success') {
          showStatus(res.message || t('knowledge_import_failed'), true)
          await refresh()
          return false
        }
        const p = res.payload
        showStatus(
          tf('knowledge_import_result', {
            imported: p?.imported ?? 0,
            skipped: p?.skipped ?? 0,
            failed: p?.failed ?? 0,
          })
        )
        const first = (p?.results || []).find((r) => r.status === 'imported')
        await refresh(first?.path)
        return true
      } catch {
        showStatus(t('knowledge_import_failed'), true)
        return false
      }
    },
    [refresh, showStatus, viewingAgentId]
  )

  // Open the import dialog after validating the chosen files.
  const startImport = useCallback(
    (files: File[]) => {
      const err = validateImportFiles(files)
      if (err) {
        showStatus(err, true)
        return
      }
      setDialog({ kind: 'import', files })
    },
    [showStatus]
  )

  const onFilesPicked = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(e.target.files || [])
      e.target.value = ''
      if (files.length) startImport(files)
    },
    [startImport]
  )

  // Rename, move and delete: answers the action's payload, or null once the
  // failure has been reported.
  const runAction = useCallback(
    async (req: KnowledgeAction): Promise<Record<string, unknown> | null> => {
      showStatus(t('knowledge_working'), false, true)
      try {
        const res = await apiClient.knowledgeAction({
          ...req,
          ...(viewingAgentId ? { agent_id: viewingAgentId } : {}),
        })
        if (res.status !== 'success') {
          showStatus((res.message as string) || t('knowledge_request_failed'), true)
          await refresh()
          return null
        }
        return (res.payload || {}) as Record<string, unknown>
      } catch {
        showStatus(t('knowledge_request_failed'), true)
        return null
      }
    },
    [refresh, showStatus, viewingAgentId]
  )

  const opensCategory = useCallback(
    (path: string) => !!activePath && activePath.startsWith(`${path}/`),
    [activePath]
  )

  // Moving or renaming changes the open page's path, which an open editor
  // would save back to, so the editor is saved and closed first - an untouched
  // one just closes. False when the user declined or the save did not land;
  // the editor stays open either way.
  const saveOpenEdit = useCallback(async (): Promise<boolean> => {
    const { edit, save } = knowledgeEditor.getState()
    if (!edit) return true
    const ok = await askConfirm({
      titleKey: 'knowledge_save_first_title',
      msgKey: 'knowledge_save_first_msg',
      okKey: 'doc_edit_save',
      tone: 'primary',
    })
    if (!ok) return false
    await save(editorRef.current?.value ?? edit.loaded)
    return !knowledgeEditor.getState().edit
  }, [])

  const startMoveDocument = useCallback(
    async (path: string) => {
      if (!moveTargets(categories, path).length) {
        showStatus(t('knowledge_move_need_category'), true)
        return
      }
      if (activePath === path && !(await saveOpenEdit())) return
      setDialog({ kind: 'move-document', path })
    },
    [activePath, categories, saveOpenEdit, showStatus]
  )

  const moveDocument = useCallback(
    async (path: string, target: string): Promise<boolean> => {
      const payload = await runAction({
        action: 'move_documents',
        payload: { paths: [path], target_category: target },
      })
      if (!payload) return false
      const result = ((payload.results as { moved?: boolean; target?: string; reason?: string }[]) || [])[0]
      if (!result?.moved) {
        const clash = result?.reason === 'target_exists'
        showStatus(clash ? t('knowledge_move_target_exists') : t('knowledge_request_failed'), true)
        await refresh()
        // A name clash can be fixed by picking another category; keep the dialog.
        return !clash
      }
      showStatus(tf('knowledge_documents_moved', { count: Number(payload.moved) || 1 }))
      await refresh(activePath === path ? result.target : undefined)
      return true
    },
    [activePath, refresh, runAction, showStatus]
  )

  // Deleting needs no separate guard: confirming the deletion also gives up any
  // unsaved edit of the page, and nothing is dropped if the user backs out.
  const deleteDocument = useCallback(
    async (path: string) => {
      const affected = activePath === path
      const ok = await askConfirm({
        titleKey: 'knowledge_delete_document',
        msgKey: 'knowledge_delete_document_confirm',
        okKey: 'knowledge_delete',
        vars: { name: path },
      })
      if (!ok) return
      const payload = await runAction({ action: 'delete_documents', payload: { paths: [path] } })
      if (!payload) return
      showStatus(tf('knowledge_documents_deleted', { count: Number(payload.deleted) || 0 }))
      if (affected) knowledgeEditor.getState().forget()
      await refresh(affected ? null : undefined)
    },
    [activePath, refresh, runAction, showStatus]
  )

  const startRenameCategory = useCallback(
    async (path: string) => {
      if (opensCategory(path) && !(await saveOpenEdit())) return
      setDialog({ kind: 'rename-category', path })
    },
    [opensCategory, saveOpenEdit]
  )

  const renameCategory = useCallback(
    async (path: string, newPath: string): Promise<boolean> => {
      const payload = await runAction({ action: 'rename_category', payload: { path, new_path: newPath } })
      if (!payload) return false
      const oldPath = (payload.old_path as string) || path
      const renamed = (payload.path as string) || newPath
      showStatus(t('knowledge_category_renamed'))
      await refresh(
        activePath && opensCategory(oldPath) ? renamed + activePath.slice(oldPath.length) : undefined
      )
      return true
    },
    [activePath, opensCategory, refresh, runAction, showStatus]
  )

  const deleteCategory = useCallback(
    async (path: string) => {
      const affected = opensCategory(path)
      const ok = await askConfirm({
        titleKey: 'knowledge_delete_category',
        msgKey: 'knowledge_delete_category_confirm',
        okKey: 'knowledge_delete',
        vars: { name: path },
      })
      if (!ok) return
      const payload = await runAction({ action: 'delete_category', payload: { path, confirm: true } })
      if (!payload) return
      showStatus(t('knowledge_category_deleted'))
      if (affected) knowledgeEditor.getState().forget()
      await refresh(affected ? null : undefined)
    },
    [opensCategory, refresh, runAction, showStatus]
  )

  const treeActions: TreeActions = {
    moveDocument: startMoveDocument,
    deleteDocument,
    renameCategory: startRenameCategory,
    deleteCategory,
  }

  const totalPages = data?.stats?.pages ?? 0
  const statsLabel = useMemo(() => {
    if (!data) return ''
    return t('knowledge_stats')
      .replace('{pages}', String(data.stats?.pages ?? 0))
      .replace('{size}', formatSize(data.stats?.size ?? 0))
  }, [data])

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-content-tertiary">
        <Loader2 size={18} className="animate-spin mr-2" />
        {t('knowledge_loading')}
      </div>
    )
  }

  const isEmpty = !data || (totalPages === 0 && !(data.root_files?.length) && !catalog?.pages.length)

  return (
    <div className="flex-1 flex flex-col min-h-0 min-w-0 max-w-full [overflow-wrap:anywhere]">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 sm:px-6 pt-5 pb-3 flex-shrink-0 min-w-0">
        <div className="min-w-0">
          <h2 className="text-xl font-bold text-content">{t('knowledge_title')}</h2>
          {!isEmpty && <p className="text-xs text-content-tertiary mt-1">{statsLabel}</p>}
        </div>
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
          {status && (
            <span
              className={`text-xs max-w-full lg:max-w-[260px] truncate ${
                status.error ? 'text-danger' : 'text-content-tertiary'
              }`}
              title={status.text}
            >
              {status.text}
            </span>
          )}
          <div className="min-w-0 max-w-full [&>div]:max-w-full [&_button]:max-w-full">
            <AgentScopeSelect value={viewingAgentId} onChange={(id) => void setScope(id)} />
          </div>
          <div className="flex min-w-0 max-w-full flex-wrap items-center gap-1 bg-inset-2 rounded-btn p-0.5">
            <TabBtn
              icon={Files}
              label={t('knowledge_tab_docs')}
              active={tab === 'docs'}
              onClick={() => void switchTab('docs')}
            />
            <TabBtn
              icon={Network}
              label={t('knowledge_tab_graph')}
              active={tab === 'graph'}
              onClick={() => void switchTab('graph')}
            />
            <TabBtn icon={Upload} label="资料" active={tab === 'personal'} onClick={() => void switchTab('personal')} />
          </div>
          <NewMenu
            open={menuOpen}
            setOpen={setMenuOpen}
            onCreateCategory={() => setDialog({ kind: 'category' })}
            onCreateDocument={() => {
              if (!categories.length) {
                showStatus(t('knowledge_need_category'), true)
                return
              }
              setDialog({ kind: 'doc-pick-category' })
            }}
            onImport={() => fileInputRef.current?.click()}
          />
        </div>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".md,.txt,text/markdown,text/plain"
          className="hidden"
          onChange={onFilesPicked}
        />
      </div>

      {tab === 'personal' ? <PersonalTasksPanel onKnowledgeChanged={onPersonalKnowledgeChanged} onOpenKnowledge={(path, title) => { setTab('docs'); openDoc(path.replace(/^knowledge\//, ''), title) }} /> : isEmpty ? (
        <div className="flex-1 flex flex-col items-center justify-center px-6 text-center border-t border-default">
          <div className="w-14 h-14 rounded-2xl bg-accent-soft text-accent flex items-center justify-center mb-5">
            <Files size={26} />
          </div>
          <h2 className="text-lg font-semibold text-content mb-2">
            {data?.enabled === false ? t('knowledge_disabled') : t('knowledge_empty')}
          </h2>
          <p className="text-sm text-content-tertiary max-w-md mb-6">{t('knowledge_empty_guide')}</p>
          <button
            onClick={() => navigate('/')}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-btn bg-accent text-accent-contrast hover:bg-accent-hover text-sm font-medium cursor-pointer transition-colors"
          >
            <MessageSquarePlus size={15} />
            {t('knowledge_go_chat')}
          </button>
        </div>
      ) : tab === 'docs' ? (
        <div
          className="flex-1 flex min-h-0 min-w-0 border-t border-default relative"
          onDragEnter={(e) => {
            if (e.dataTransfer?.types?.includes('Files')) {
              e.preventDefault()
              setDragOver(true)
            }
          }}
          onDragOver={(e) => {
            if (e.dataTransfer?.types?.includes('Files')) e.preventDefault()
          }}
          onDragLeave={(e) => {
            // Only clear when leaving the panel, not its children.
            if (e.currentTarget === e.target) setDragOver(false)
          }}
          onDrop={(e) => {
            e.preventDefault()
            setDragOver(false)
            const files = Array.from(e.dataTransfer?.files || [])
            if (files.length) startImport(files)
          }}
        >
          {dragOver && (
            <div className="absolute inset-0 z-30 flex items-center justify-center bg-accent-soft/80 border-2 border-dashed border-accent rounded-lg m-2 pointer-events-none">
              <div className="flex flex-col items-center gap-2 text-accent">
                <Upload size={28} />
                <p className="text-sm font-medium">{t('knowledge_drop_hint')}</p>
              </div>
            </div>
          )}
          {/* Tree sidebar */}
          <div className={`w-full lg:w-64 min-w-0 flex-shrink-0 flex-col lg:border-r border-default min-h-0 ${doc || historyPublishId ? 'hidden lg:flex' : 'flex'}`}>
            <div className="p-3 flex-shrink-0">
              <div className="relative">
                <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-content-tertiary" />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t('knowledge_search')}
                  className="w-full pl-8 pr-3 py-2 rounded-btn border border-strong bg-inset text-sm text-content placeholder:text-content-tertiary focus:outline-none focus:border-accent transition-colors"
                />
              </div>
            </div>
            <div className="flex-1 overflow-y-auto px-2 pb-3">
              <div className="mb-4 space-y-1">
                {[['index.md', '知识索引'], ['log.md', '整理记录']].map(([path, title]) => <button key={path} type="button" onClick={() => openDoc(path, title)}
                  className={`w-full rounded-btn px-3 py-2 text-left text-sm ${activePath === path && !historyPublishId ? 'bg-accent-soft text-accent' : 'text-content-secondary hover:bg-surface-2'}`}>{title}</button>)}
              </div>
              {catalogError && <div role="alert" className="mb-3 px-2 text-xs text-danger">{catalogError}<button type="button" onClick={() => void refresh()} className="ml-2 underline">重试</button></div>}
              <KnowledgeCatalogGroups pages={catalog?.pages || []} search={search} activePath={historyPublishId ? null : activePath} onOpen={openDoc} compact />
              {legacyTree && <div className="mt-4 border-t border-default pt-3"><Tree
                data={{ ...legacyTree, root_files: legacyTree.root_files?.filter(f => !PROTECTED_KNOWLEDGE_PAGES.includes(f.name)) }}
                search={search.trim().toLowerCase()}
                activePath={activePath}
                onOpen={openDoc}
                actions={treeActions}
              /></div>}
            </div>
          </div>

          {/* Document viewer / editor */}
          <div ref={viewerRef} className={`flex-1 min-w-0 flex-col min-h-0 ${doc || historyPublishId ? 'flex' : 'hidden lg:flex'}`}>
            <button type="button" onClick={() => void returnToDirectory()}
              className="m-3 inline-flex w-fit max-w-full items-center gap-1 text-sm text-content-secondary lg:hidden"><ArrowLeft size={14} />返回目录</button>
            {historyPublishId ? <div className="flex-1 overflow-y-auto">
              <KnowledgeVersionView key={`${viewingAgentId}:${historyPublishId}`} publishId={historyPublishId} agentId={viewingAgentId || undefined}
                anchor={pendingAnchor?.anchor} onAnchorResolved={found => { setPendingAnchor(null); if (!found) showStatus('已打开历史版本，未找到指定位置', true) }}
                onBack={() => openDoc('log.md', '整理记录')} onOpenCurrent={openDoc} onInternalLink={openInternalLink} />
            </div> : !doc ? (
              <div className="h-full flex flex-col items-center justify-center text-content-tertiary">
                <FileText size={28} className="mb-3 opacity-50" />
                <p className="text-sm">{t('knowledge_select_hint')}</p>
              </div>
            ) : (
              <>
                {!PROTECTED_KNOWLEDGE_PAGES.includes(doc.path) && <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 sm:px-6 py-3 flex-shrink-0 min-w-0 border-b border-subtle">
                  <h3 className="basis-full lg:basis-0 lg:flex-1 min-w-0 text-base font-semibold text-content truncate">
                    {docTitle}
                    {edit?.dirty && (
                      <span className="text-accent" title={t('ws_edit_unsaved')}>
                        {' '}
                        •
                      </span>
                    )}
                  </h3>
                  <span
                    className={`text-xs text-content-tertiary truncate max-w-full lg:max-w-[45%] ${
                      readonly ? '' : 'font-mono'
                    }`}
                    title={readonly || undefined}
                  >
                    {currentPage?.category || (activePath ? displayCategory(activePath) : '')}
                  </span>
                  {currentPage && <KnowledgeCategoryMenu key={currentPage.page_id} page={currentPage} categories={catalog?.categories || []} onChange={changePageCategory} />}
                  <DocActions store={knowledgeEditor} textareaRef={editorRef} />
                </div>}
                <DocNotice store={knowledgeEditor} />
                {doc.path === 'index.md' ? <div className="flex-1 overflow-y-auto">
                  {catalogError && <p role="alert" className="px-6 pt-4 text-sm text-danger">{catalogError}</p>}
                  <KnowledgeCatalogView pages={catalog?.pages || []} onOpen={openDoc} legacyContent={catalog?.legacy_content}
                    onInternalLink={href => openInternalLink(href, 'index.md')} imageBaseDir={doc.dir} />
                </div> : doc.path === 'log.md' ? <div className="flex-1 overflow-y-auto">
                  <KnowledgeHistory key={viewingAgentId} agentId={viewingAgentId || undefined} revision={knowledgeRevision}
                    onOpenVersion={id => { navigationSequence.current++; setPendingAnchor(null); setHistoryPublishId(id) }} onInternalLink={href => openInternalLink(href, 'log.md')} />
                </div> : edit ? (
                  // Keyed so switching the edited page remounts the text area
                  // and reseeds it, instead of keeping the previous page's text.
                  <div className="flex-1 min-h-0 overflow-hidden">
                    <DocEditor key={doc.path} store={knowledgeEditor} textareaRef={editorRef} />
                  </div>
                ) : (
                  <DocView store={knowledgeEditor}>
                    <div className="min-w-0 max-w-3xl mx-auto px-4 sm:px-6 py-6">
                      {docLoading ? (
                        <div className="flex items-center text-content-tertiary py-8">
                          <Loader2 size={16} className="animate-spin mr-2" />
                        </div>
                      ) : (
                        <Markdown
                          content={stripDuplicateH1(content, docTitle)}
                          onInternalLink={href => openInternalLink(href, doc.path)}
                          imageBaseDir={doc.dir}
                        />
                      )}
                    </div>
                  </DocView>
                )}
              </>
            )}
          </div>
        </div>
      ) : (
        <div className="flex-1 min-h-0 border-t border-default relative">
          {graphLoading ? (
            <div className="absolute inset-0 flex items-center justify-center text-content-tertiary">
              <Loader2 size={18} className="animate-spin mr-2" />
            </div>
          ) : graph && graph.nodes.length > 0 ? (
            <KnowledgeGraph data={graph} onSelect={onGraphSelect} />
          ) : (
            <div className="absolute inset-0 flex items-center justify-center text-content-tertiary text-sm">
              {t('knowledge_graph_empty')}
            </div>
          )}
        </div>
      )}

      {dialog && (
        <KnowledgeDialog
          state={dialog}
          categories={categories}
          onClose={() => setDialog(null)}
          onCreateCategory={createCategory}
          onPickDocCategory={(category) => setDialog({ kind: 'document', category })}
          onCreateDocument={createDocument}
          onImport={importDocuments}
          onRenameCategory={renameCategory}
          onMoveDocument={moveDocument}
        />
      )}
    </div>
  )
}

// ---- New menu --------------------------------------------------------------

const NewMenu: React.FC<{
  open: boolean
  setOpen: (v: boolean) => void
  onCreateCategory: () => void
  onCreateDocument: () => void
  onImport: () => void
}> = ({ open, setOpen, onCreateCategory, onCreateDocument, onImport }) => {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onClickOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [open, setOpen])

  const pick = (fn: () => void) => {
    setOpen(false)
    fn()
  }

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent text-accent-contrast hover:bg-accent-hover text-sm font-medium cursor-pointer transition-colors"
      >
        <Plus size={14} />
        {t('knowledge_new')}
        <ChevronDown size={12} className="opacity-80" />
      </button>
      {open && (
        <div className="absolute right-0 mt-1.5 w-44 z-50 bg-surface border border-default rounded-lg shadow-lg py-1">
          <MenuItem icon={FolderPlus} label={t('knowledge_new_category')} onClick={() => pick(onCreateCategory)} />
          <MenuItem icon={FilePlus2} label={t('knowledge_new_document')} onClick={() => pick(onCreateDocument)} />
          <MenuItem icon={Upload} label={t('knowledge_import_documents')} onClick={() => pick(onImport)} />
        </div>
      )}
    </div>
  )
}

const MenuItem: React.FC<{ icon: LucideIcon; label: string; onClick: () => void }> = ({
  icon: Icon,
  label,
  onClick,
}) => (
  <button
    onClick={onClick}
    className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-content-secondary hover:bg-surface-2 cursor-pointer transition-colors text-left"
  >
    <Icon size={14} className="opacity-70 flex-shrink-0" />
    {label}
  </button>
)

// ---- Dialog ----------------------------------------------------------------

function templateFor(filename: string): string {
  const title = (filename || 'untitled').replace(/\.md$/i, '')
  return getLang() === 'zh'
    ? `# ${title}\n\n## 摘要\n\n\n## 关键点\n\n- \n\n## 参考\n\n`
    : `# ${title}\n\n## Summary\n\n\n## Key points\n\n- \n\n## References\n\n`
}

const KnowledgeDialog: React.FC<{
  state: DialogState
  categories: string[]
  onClose: () => void
  onCreateCategory: (path: string) => Promise<string | null>
  onPickDocCategory: (category: string) => void
  onCreateDocument: (path: string, content: string) => Promise<string | null>
  onImport: (files: File[], target: string) => Promise<boolean>
  onRenameCategory: (path: string, newPath: string) => Promise<boolean>
  onMoveDocument: (path: string, target: string) => Promise<boolean>
}> = ({
  state,
  categories,
  onClose,
  onCreateCategory,
  onPickDocCategory,
  onCreateDocument,
  onImport,
  onRenameCategory,
  onMoveDocument,
}) => {
  const choices = state.kind === 'move-document' ? moveTargets(categories, state.path || '') : categories
  const [categoryInput, setCategoryInput] = useState(state.kind === 'rename-category' ? state.path || '' : '')
  const [selected, setSelected] = useState(choices[0] || '')
  const [filename, setFilename] = useState('')
  const [contentInput, setContentInput] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    // Enter in a text field bypasses the disabled Confirm button.
    if (busy) return
    setError('')
    if (state.kind === 'category') {
      const path = categoryInput.trim()
      if (!path) return setError(t('knowledge_field_required'))
      setBusy(true)
      const ok = await onCreateCategory(path)
      setBusy(false)
      if (ok !== null) onClose()
      return
    }
    if (state.kind === 'doc-pick-category') {
      if (!selected) return setError(t('knowledge_field_required'))
      onPickDocCategory(selected)
      return
    }
    if (state.kind === 'document') {
      const name = filename.trim()
      if (!name) return setError(t('knowledge_doc_filename_required'))
      if (/\.[^.]+$/i.test(name) && !/\.md$/i.test(name)) return setError(t('knowledge_doc_must_md'))
      if (!contentInput.trim()) return setError(t('knowledge_doc_content_required'))
      if (new Blob([contentInput]).size > KNOWLEDGE_IMPORT_MAX_FILE_SIZE) {
        return setError(t('knowledge_doc_content_too_large'))
      }
      const safeName = name.endsWith('.md') ? name : `${name}.md`
      setBusy(true)
      const ok = await onCreateDocument(`${state.category}/${safeName}`, contentInput)
      setBusy(false)
      if (ok !== null) onClose()
      return
    }
    if (state.kind === 'import') {
      if (!selected) return setError(t('knowledge_field_required'))
      setBusy(true)
      const ok = await onImport(state.files || [], selected)
      setBusy(false)
      if (ok) onClose()
      return
    }
    if (state.kind === 'rename-category') {
      const newPath = categoryInput.trim()
      if (!newPath) return setError(t('knowledge_field_required'))
      if (newPath === state.path) return setError(t('knowledge_rename_same'))
      setBusy(true)
      const ok = await onRenameCategory(state.path || '', newPath)
      setBusy(false)
      if (ok) onClose()
      return
    }
    if (state.kind === 'move-document') {
      if (!selected) return setError(t('knowledge_field_required'))
      setBusy(true)
      const ok = await onMoveDocument(state.path || '', selected)
      setBusy(false)
      if (ok) onClose()
      return
    }
  }

  const titleMap: Record<DialogState['kind'], string> = {
    category: t('knowledge_new_category'),
    'doc-pick-category': t('knowledge_new_document'),
    document: t('knowledge_new_document'),
    import: t('knowledge_import_documents'),
    'rename-category': t('knowledge_rename_category'),
    'move-document': t('knowledge_move_document'),
  }

  const noCategory =
    (state.kind === 'doc-pick-category' || state.kind === 'import' || state.kind === 'move-document') &&
    !choices.length

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 p-4" onMouseDown={onClose}>
      <div
        className="w-full max-w-lg bg-surface border border-default rounded-xl shadow-xl p-5"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold text-content mb-1">{titleMap[state.kind]}</h3>

        {state.kind === 'category' && (
          <>
            <p className="text-xs text-content-tertiary mb-4">{t('knowledge_category_subtitle')}</p>
            <label className="block text-sm text-content-secondary mb-1.5">{t('knowledge_category_label')}</label>
            <input
              autoFocus
              value={categoryInput}
              onChange={(e) => setCategoryInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && submit()}
              placeholder="research/ai"
              className="w-full px-3 py-2 rounded-btn border border-strong bg-inset text-sm text-content placeholder:text-content-tertiary focus:outline-none focus:border-accent transition-colors"
            />
            <p className="text-xs text-content-tertiary mt-1.5">{t('knowledge_category_hint')}</p>
          </>
        )}

        {state.kind === 'doc-pick-category' && (
          <>
            <p className="text-xs text-content-tertiary mb-4">{t('knowledge_doc_choose_category')}</p>
            <label className="block text-sm text-content-secondary mb-1.5">{t('knowledge_destination')}</label>
            <CategorySelect value={selected} options={categories} onChange={setSelected} />
          </>
        )}

        {state.kind === 'document' && (
          <>
            <p className="text-xs text-content-tertiary mb-4">
              {tf('knowledge_doc_save_to', { category: state.category || '' })}
            </p>
            <label className="block text-sm text-content-secondary mb-1.5">{t('knowledge_doc_filename')}</label>
            <input
              autoFocus
              value={filename}
              onChange={(e) => setFilename(e.target.value)}
              placeholder="my-note.md"
              className="w-full px-3 py-2 rounded-btn border border-strong bg-inset text-sm text-content placeholder:text-content-tertiary focus:outline-none focus:border-accent transition-colors mb-3"
            />
            <div className="flex items-center justify-between mb-1.5">
              <label className="text-sm text-content-secondary">{t('knowledge_doc_content')}</label>
              <button
                onClick={() => {
                  if (!contentInput.trim()) setContentInput(templateFor(filename))
                }}
                className="text-xs text-accent hover:underline cursor-pointer"
              >
                {t('knowledge_doc_insert_template')}
              </button>
            </div>
            <textarea
              value={contentInput}
              onChange={(e) => setContentInput(e.target.value)}
              rows={10}
              className="w-full px-3 py-2 rounded-btn border border-strong bg-inset text-sm text-content placeholder:text-content-tertiary focus:outline-none focus:border-accent transition-colors font-mono resize-y"
            />
          </>
        )}

        {state.kind === 'import' && (
          <>
            <p className="text-xs text-content-tertiary mb-4">
              {tf('knowledge_import_selected', { count: state.files?.length ?? 0 })}
            </p>
            <label className="block text-sm text-content-secondary mb-1.5">{t('knowledge_destination')}</label>
            <CategorySelect value={selected} options={categories} onChange={setSelected} />
            <p className="text-xs text-content-tertiary mt-1.5">
              {categories.length ? t('knowledge_import_hint') : t('knowledge_import_need_category')}
            </p>
          </>
        )}

        {state.kind === 'rename-category' && (
          <>
            <p className="text-xs text-content-tertiary mb-4 font-mono truncate">{state.path}</p>
            <label className="block text-sm text-content-secondary mb-1.5">{t('knowledge_rename_label')}</label>
            <input
              autoFocus
              value={categoryInput}
              onChange={(e) => setCategoryInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && submit()}
              className="w-full px-3 py-2 rounded-btn border border-strong bg-inset text-sm text-content placeholder:text-content-tertiary focus:outline-none focus:border-accent transition-colors"
            />
            <p className="text-xs text-content-tertiary mt-1.5">{t('knowledge_category_hint')}</p>
          </>
        )}

        {state.kind === 'move-document' && (
          <>
            <p className="text-xs text-content-tertiary mb-4 font-mono truncate">{state.path}</p>
            <label className="block text-sm text-content-secondary mb-1.5">{t('knowledge_destination')}</label>
            <CategorySelect value={selected} options={choices} onChange={setSelected} />
          </>
        )}

        {error && <p className="text-xs text-danger mt-3">{error}</p>}

        <div className="flex justify-end gap-2 mt-5">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-btn border border-strong text-content-secondary hover:bg-surface-2 text-sm font-medium cursor-pointer transition-colors"
          >
            {t('knowledge_dialog_cancel')}
          </button>
          <button
            onClick={submit}
            disabled={busy || noCategory}
            className="px-4 py-2 rounded-btn bg-accent text-accent-contrast hover:bg-accent-hover text-sm font-medium cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
          >
            {busy && <Loader2 size={14} className="animate-spin" />}
            {t('knowledge_dialog_confirm')}
          </button>
        </div>
      </div>
    </div>
  )
}

// Custom dropdown: keeps the arrow / menu styling consistent with the rest of
// the desktop UI (a native <select> renders an OS arrow we can't space out).
const CategorySelect: React.FC<{ value: string; options: string[]; onChange: (v: string) => void }> = ({
  value,
  options,
  onChange,
}) => {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onClickOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [open])

  const disabled = !options.length

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className={`w-full flex items-center justify-between gap-2 px-3 py-2 rounded-btn border text-sm text-content transition-colors cursor-pointer ${
          open ? 'border-accent' : 'border-strong'
        } bg-inset hover:border-accent/70 disabled:opacity-50 disabled:cursor-not-allowed`}
      >
        <span className="truncate">{value || '--'}</span>
        <ChevronDown
          size={14}
          className={`flex-shrink-0 text-content-tertiary transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>
      {open && (
        <div className="absolute left-0 right-0 top-full mt-1 z-50 max-h-60 overflow-y-auto bg-surface border border-default rounded-lg shadow-lg p-1">
          {options.map((opt) => (
            <button
              key={opt}
              type="button"
              onClick={() => {
                onChange(opt)
                setOpen(false)
              }}
              className={`w-full text-left px-2.5 py-1.5 rounded-md text-sm cursor-pointer transition-colors truncate ${
                opt === value ? 'bg-accent-soft text-accent' : 'text-content-secondary hover:bg-surface-2'
              }`}
            >
              {opt}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

const TabBtn: React.FC<{
  icon: LucideIcon
  label: string
  active: boolean
  onClick: () => void
}> = ({ icon: Icon, label, active, onClick }) => (
  <button
    onClick={onClick}
    className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-[6px] text-sm font-medium cursor-pointer transition-colors ${
      active ? 'bg-elevated dark:bg-white/10 text-content shadow-sm' : 'text-content-tertiary hover:text-content-secondary'
    }`}
  >
    <Icon size={14} />
    {label}
  </button>
)

// ---- Tree rendering --------------------------------------------------------

interface TreeActions {
  moveDocument: (path: string) => void
  deleteDocument: (path: string) => void
  renameCategory: (path: string) => void
  deleteCategory: (path: string) => void
}

// Revealed on row hover. A span rather than a button: the row it sits in is
// itself a button.
const RowAction: React.FC<{
  icon: LucideIcon
  label: string
  danger?: boolean
  onClick: () => void
}> = ({ icon: Icon, label, danger, onClick }) => (
  <Tooltip label={label}>
    <span
      role="button"
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation()
        onClick()
      }}
      className={`inline-flex items-center justify-center w-6 h-6 rounded cursor-pointer transition-colors text-content-tertiary ${
        danger ? 'hover:text-danger hover:bg-danger-soft' : 'hover:text-content hover:bg-surface'
      }`}
    >
      <Icon size={12} />
    </span>
  </Tooltip>
)

const Tree: React.FC<{
  data: KnowledgeList
  search: string
  activePath: string | null
  onOpen: (path: string, title: string) => void
  actions: TreeActions
}> = ({ data, search, activePath, onOpen, actions }) => {
  const matches = (f: KnowledgeFile) => !search || f.title.toLowerCase().includes(search) || f.name.toLowerCase().includes(search)

  return (
    <div className="space-y-0.5">
      {(data.root_files || []).filter(matches).map((f) => (
        <FileLeaf
          key={f.name}
          path={f.name}
          title={f.title || f.name}
          active={activePath === f.name}
          onOpen={onOpen}
          actions={actions}
        />
      ))}
      {(data.tree || []).map((dir) => (
        <DirNode
          key={dir.dir}
          dir={dir}
          prefix=""
          search={search}
          activePath={activePath}
          onOpen={onOpen}
          actions={actions}
        />
      ))}
    </div>
  )
}

// Count files in a dir subtree that match the search.
function countMatches(dir: KnowledgeDir, search: string): number {
  const own = dir.files.filter(
    (f) => !search || f.title.toLowerCase().includes(search) || f.name.toLowerCase().includes(search)
  ).length
  return own + dir.children.reduce((acc, c) => acc + countMatches(c, search), 0)
}

const DirNode: React.FC<{
  dir: KnowledgeDir
  prefix: string
  search: string
  activePath: string | null
  onOpen: (path: string, title: string) => void
  actions: TreeActions
}> = ({ dir, prefix, search, activePath, onOpen, actions }) => {
  const dirPath = prefix ? `${prefix}/${dir.dir}` : dir.dir
  const [open, setOpen] = useState(true)
  const matchCount = search ? countMatches(dir, search) : dir.files.length + dir.children.length
  if (search && matchCount === 0) return null

  const visibleFiles = dir.files.filter(
    (f) => !search || f.title.toLowerCase().includes(search) || f.name.toLowerCase().includes(search)
  )
  const expanded = open || !!search

  // 保留真实路径用于打开页面，只隐藏存储层的内部目录。
  if (dirPath === 'wiki' || dirPath.startsWith('managed')) return <>
    {visibleFiles.map(f => <FileLeaf key={`${dirPath}/${f.name}`} path={`${dirPath}/${f.name}`} title={f.title} active={activePath === `${dirPath}/${f.name}`} onOpen={onOpen} actions={actions} />)}
    {dir.children.map(c => <DirNode key={c.dir} dir={c} prefix={dirPath} search={search} activePath={activePath} onOpen={onOpen} actions={actions} />)}
  </>

  return (
    <div>
      <button
        onClick={() => setOpen((v) => !v)}
        className="group w-full flex items-center gap-1 px-2 py-1.5 rounded-btn text-sm text-content-secondary hover:bg-surface-2 cursor-pointer transition-colors"
      >
        {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <span className="truncate font-medium">{dir.dir}</span>
        <span className="ml-auto text-xs text-content-tertiary group-hover:hidden">
          {matchCount}
        </span>
        {!/^(wiki|managed)(\/|$)/.test(dirPath) && <span className="ml-auto -my-1 hidden group-hover:flex items-center gap-0.5 flex-shrink-0">
          <RowAction icon={Pencil} label={t('knowledge_rename')} onClick={() => actions.renameCategory(dirPath)} />
          <RowAction
            icon={Trash2}
            label={t('knowledge_delete')}
            danger
            onClick={() => actions.deleteCategory(dirPath)}
          />
        </span>}
      </button>
      {expanded && (
        <div className="ml-3 border-l border-default pl-1.5 space-y-0.5">
          {visibleFiles.map((f) => {
            const fpath = `${dirPath}/${f.name}`
            return (
              <FileLeaf
                key={fpath}
                path={fpath}
                title={f.title || f.name}
                active={activePath === fpath}
                onOpen={onOpen}
                actions={actions}
              />
            )
          })}
          {dir.children.map((c) => (
            <DirNode
              key={c.dir}
              dir={c}
              prefix={dirPath}
              search={search}
              activePath={activePath}
              onOpen={onOpen}
              actions={actions}
            />
          ))}
        </div>
      )}
    </div>
  )
}

const FileLeaf: React.FC<{
  path: string
  title: string
  active: boolean
  onOpen: (path: string, title: string) => void
  actions: TreeActions
}> = ({ path, title, active, onOpen, actions }) => (
  <button
    onClick={() => onOpen(path, title)}
    className={`group w-full flex items-center gap-2 px-2 py-1.5 rounded-btn text-[13px] cursor-pointer transition-colors text-left ${
      active ? 'bg-accent-soft text-accent' : 'text-content-secondary hover:bg-surface-2'
    }`}
  >
    <FileText size={13} className="flex-shrink-0 opacity-70" />
    <span className="truncate">{title}</span>
    {!isProtectedPage(path) && (
      <span className="ml-auto -my-1 hidden group-hover:flex items-center gap-0.5 flex-shrink-0">
        <RowAction icon={ArrowRightLeft} label={t('knowledge_move')} onClick={() => actions.moveDocument(path)} />
        <RowAction icon={Trash2} label={t('knowledge_delete')} danger onClick={() => actions.deleteDocument(path)} />
      </span>
    )}
  </button>
)

export default KnowledgePage
