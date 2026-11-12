import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const require = createRequire(import.meta.url)
const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL('../src/main/updater.ts', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs',
  external: ['electron', 'electron-updater'],
  plugins: [{ name: 'app-config', setup(builder) {
    builder.onResolve({ filter: /^\.\/themes$/ }, () => ({ path: 'themes', namespace: 'fixture' }))
    builder.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({
      contents: 'export const loadAppConfig = () => globalThis.appConfig;',
    }))
  } }],
})

function harness(appConfig, failFirst = false) {
  const calls = { checks: 0, downloads: 0, installs: 0, feeds: [], events: [], statuses: [] }
  const updater = {
    setFeedURL: feed => calls.feeds.push(feed.url),
    on: event => calls.events.push(event),
    checkForUpdates: () => {
      calls.checks++
      return failFirst && calls.checks === 1 ? Promise.reject(new Error('fixture outage')) : Promise.resolve({})
    },
    downloadUpdate: () => { calls.downloads++; return Promise.resolve() },
    quitAndInstall: () => { calls.installs++ },
  }
  const module = { exports: {} }
  const context = vm.createContext({
    module, exports: module.exports, appConfig, process,
    console: { log() {} },
    require: id => {
      if (id === 'electron') return { app: { isPackaged: true, getVersion: () => '2.2.2', getPath: () => '/fixture' } }
      if (id === 'electron-updater') return { autoUpdater: updater }
      if (id === 'fs') return { mkdirSync() {}, appendFileSync() {}, readFileSync() { throw new Error('no packaged feed') } }
      return require(id)
    },
  })
  vm.runInContext(outputFiles[0].text, context)
  const api = module.exports
  api.initUpdater(() => ({ webContents: { send: (_channel, status) => calls.statuses.push(status.state) } }))
  return { api, calls }
}

test('Personal 的检查、下载和安装不会访问官方更新服务', async () => {
  const config = JSON.parse(readFileSync(new URL('../flavors/personal/app-config.json', import.meta.url), 'utf8'))
  const { api, calls } = harness(config)
  api.setUpdateLanguage('zh')
  api.checkForUpdates()
  api.checkForUpdates(true)
  api.startDownload()
  api.quitAndInstall()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(calls.checks, 0)
  assert.equal(calls.downloads, 0)
  assert.equal(calls.installs, 0)
  assert.deepEqual(calls.events, [])
  assert.deepEqual(calls.statuses, ['not-available', 'not-available'])
})

test('标准渠道故障会切换域名，自定义渠道保持自己的地址', async () => {
  const standard = harness(null, true)
  standard.api.checkForUpdates(true)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(standard.calls.checks, 2)
  assert.equal(new URL(standard.calls.feeds.at(-1)).hostname, 'cowagent.cn')
  assert.deepEqual(standard.calls.statuses, [])

  const custom = harness({ updateFeedUrl: 'https://updates.example.invalid/personal/' }, true)
  custom.api.checkForUpdates(true)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(custom.calls.checks, 1)
  assert.ok(custom.calls.feeds.every(url => new URL(url).hostname === 'updates.example.invalid'))
  assert.deepEqual(custom.calls.statuses, ['error'])
})
