import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

globalThis.__presetLang = 'en'
const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL('../src/renderer/src/components/ApiBasePresets.tsx', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs', external: ['react', 'react/jsx-runtime'],
  plugins: [{ name: 'offline-language', setup(builder) {
    builder.onResolve({ filter: /\/i18n$/ }, () => ({ path: 'i18n', namespace: 'fixture' }))
    builder.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({ contents:
      'export const getLang = () => globalThis.__presetLang; export const localizedLabel = x => x; export const t = x => x;' }))
  } }],
})
const module = { exports: {} }
new Function('require', 'module', 'exports', outputFiles[0].text)(createRequire(import.meta.url), module, module.exports)
const { initialApiBase, activeBasePreset, orderedBasePresets } = module.exports
const cn = 'https://api.cowagent.cn/v1'
const global = 'https://api.cowagent.ai/v1'
const presets = [{ value: cn, keys_url: 'https://cloud.cowagent.cn/keys' },
  { value: global, keys_url: 'https://cloud.cowagent.ai/keys' }]
const provider = { configured: false, api_base: cn, api_base_default: cn, api_base_presets: presets }

test('无key且显式国内站等于默认值时，英文界面保留国内站和key入口', () => {
  globalThis.__presetLang = 'en'
  const base = initialApiBase({ ...provider, configured_api_base: cn })
  assert.equal(base, cn)
  assert.equal(activeBasePreset(presets, base).keys_url, 'https://cloud.cowagent.cn/keys')
})

test('显式全球站和自定义地址均优先于中文语言预设', () => {
  globalThis.__presetLang = 'zh'
  for (const base of [global, 'https://gateway.example.test/custom/v1']) {
    assert.equal(initialApiBase({ ...provider, api_base: cn, configured_api_base: base }), base)
  }
})

test('明确未填写且无key才按当前语言预填，已有key保留有效地址', () => {
  for (const [lang, expected] of [['en', global], ['zh', cn]]) {
    globalThis.__presetLang = lang
    assert.equal(initialApiBase({ ...provider, configured_api_base: '' }), expected)
    assert.equal(initialApiBase({ ...provider, configured: true, api_base: global, configured_api_base: '' }), global)
  }
})

test('旧Provider无元数据不猜测默认值是否显式，普通Provider和空列表兼容', () => {
  globalThis.__presetLang = 'en'
  assert.equal(initialApiBase(provider), cn)
  assert.equal(initialApiBase({ ...provider, api_base_presets: undefined, configured_api_base: '' }), cn)
  assert.equal(initialApiBase(undefined), '')
  assert.deepEqual(orderedBasePresets(presets).map(p => p.value), [global, cn])
  assert.deepEqual(presets.map(p => p.value), [cn, global])
})
