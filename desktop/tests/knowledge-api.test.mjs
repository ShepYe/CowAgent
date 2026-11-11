import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const result = await build({ entryPoints: [fileURLToPath(new URL('../src/renderer/src/api/client.ts', import.meta.url))], bundle: true, write: false, platform: 'node', format: 'cjs',
  plugins: [{ name: 'offline-language', setup(b) {
    b.onResolve({ filter: /\/i18n$/ }, args => ({ path: args.path, namespace: 'offline' }))
    b.onLoad({ filter: /.*/, namespace: 'offline' }, () => ({ contents: 'export const t=x=>x; export const getLang=()=>"zh";' }))
  } }] })
const module = { exports: {} }
new Function('require', 'module', 'exports', result.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports)
const client = module.exports.default
const requests = []
const request = client.request.bind(client)
client.request = async (path, options) => { requests.push({ path, options }); return { status: 'success' } }

test('目录与真实分页事件使用指定Agent范围', async () => {
  requests.length = 0
  await client.getKnowledgeCatalog('agent A')
  await client.getKnowledgeHistory(40, 20, 'agent A')
  assert.deepEqual(requests.map(r => r.path), ['/api/knowledge/catalog?agent_id=agent%20A', '/api/knowledge/history?offset=40&limit=20&agent_id=agent%20A'])
})

test('历史版本id与精确path分别请求，中文及空格保持编码边界', async () => {
  requests.length = 0
  await client.getKnowledgeVersion('old-pub', 'agent')
  const path = 'wiki/射频/旧 主题-pub.md'
  await client.getKnowledgeVersionByPath(path, 'agent')
  const urls = requests.map(r => new URL(r.path, 'http://offline.invalid'))
  assert.equal(urls[0].searchParams.get('publish_id'), 'old-pub')
  assert.equal(urls[0].searchParams.has('path'), false)
  assert.equal(urls[1].searchParams.get('path'), path)
  assert.equal(urls[1].searchParams.has('publish_id'), false)
  assert.equal(urls[1].searchParams.get('agent_id'), 'agent')
})

test('分类动作原样传双修订和显式Agent，恢复自动不修改当前分类', async () => {
  requests.length = 0
  client.setActiveAgentId('chat-agent')
  const payload = { page_id: 'p', expected_version: 'pub', expected_category_revision: 3 }
  await client.knowledgeAction({ action: 'set_page_category', agent_id: 'view-agent', payload: { ...payload, category: '工程/射频' } })
  await client.knowledgeAction({ action: 'reset_page_category', agent_id: 'view-agent', payload })
  const bodies = requests.map(r => JSON.parse(r.options.body))
  assert.deepEqual(bodies, [
    { action: 'set_page_category', agent_id: 'view-agent', payload: { ...payload, category: '工程/射频' } },
    { action: 'reset_page_category', agent_id: 'view-agent', payload },
  ])
  client.setActiveAgentId('')
})

test.after(() => { client.request = request })
