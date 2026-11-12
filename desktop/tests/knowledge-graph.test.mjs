import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import React from 'react'

const output = await build({
  entryPoints: [fileURLToPath(new URL('../src/renderer/src/components/KnowledgeGraph.tsx', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs',
  external: ['react', 'react/jsx-runtime'],
})
const module = { exports: {} }
new Function('require', 'module', 'exports', output.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports)
const KnowledgeGraph = module.exports.default

// 驱动真实组件的钩子、DOM ref、布局/被动 effect 和动画帧；所有数据均为内存夹具。
function environment() {
  let nextFrame = 0
  const frames = new Map(), observers = new Set()
  const originals = Object.fromEntries(['requestAnimationFrame', 'cancelAnimationFrame', 'ResizeObserver'].map(k => [k, globalThis[k]]))
  globalThis.requestAnimationFrame = fn => { frames.set(++nextFrame, fn); return nextFrame }
  globalThis.cancelAnimationFrame = id => frames.delete(id)
  globalThis.ResizeObserver = class {
    constructor(fn) { this.fn = fn; observers.add(this) }
    observe() {}
    disconnect() { observers.delete(this) }
  }
  return {
    frames,
    step() { const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn()) },
    settle() { let limit = 1000; while (frames.size && limit-- > 0) this.step(); assert.ok(limit > 0, '布局应当收敛') },
    resize() { observers.forEach(o => o.fn()) },
    restore() { Object.assign(globalThis, originals) },
  }
}

function mount(data, onSelect = () => {}) {
  const slots = [], dom = new Map()
  let cursor = 0, dirty = false, tree, props = { data, onSelect }
  let layout = [], passive = []
  const effect = (queue, fn, deps) => {
    const i = cursor++, old = slots[i]
    if (!old || !deps || deps.some((d, j) => !Object.is(d, old.deps?.[j]))) {
      queue.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: fn() } })
    }
  }
  const dispatcher = {
    useState(initial) {
      const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial
      return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; dirty = true }]
    },
    useRef(initial) { const i = cursor++; return slots[i] ||= { current: initial } },
    useMemo(fn, deps) {
      const i = cursor++, old = slots[i]
      if (!old || deps.some((d, j) => !Object.is(d, old.deps[j]))) slots[i] = { deps, value: fn() }
      return slots[i].value
    },
    useEffect(fn, deps) { effect(passive, fn, deps) },
    useLayoutEffect(fn, deps) { effect(layout, fn, deps) },
  }
  function attach(node, key = 'root') {
    if (Array.isArray(node)) { node.forEach((n, i) => attach(n, `${key}/${i}`)); return }
    if (!node || typeof node !== 'object') return
    let el = dom.get(key)
    if (!el) {
      const attrs = {}, listeners = new Map()
      el = { attrs, listeners, clientWidth: 800, clientHeight: 560,
        setAttribute: (k, v) => { attrs[k] = v },
        addEventListener: (k, fn) => listeners.set(k, fn),
        removeEventListener: (k, fn) => { if (listeners.get(k) === fn) listeners.delete(k) },
        getBoundingClientRect: () => ({ left: 0, top: 0 }),
        setPointerCapture() {}, releasePointerCapture() {},
      }
      dom.set(key, el)
    }
    el.node = node
    if (node.props.transform) el.attrs.transform = node.props.transform
    if (typeof node.ref === 'function') node.ref(el)
    else if (node.ref) node.ref.current = el
    attach(node.props.children, `${key}/children`)
  }
  const h = {
    flush() {
      do {
        dirty = false; cursor = 0
        const internals = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED
        const previous = internals.ReactCurrentDispatcher.current
        internals.ReactCurrentDispatcher.current = dispatcher
        try { tree = KnowledgeGraph(props) } finally { internals.ReactCurrentDispatcher.current = previous }
        attach(tree)
        const l = layout; layout = []; l.forEach(fn => fn())
        const p = passive; passive = []; p.forEach(fn => fn())
      } while (dirty)
      return h
    },
    update(data) { props = { ...props, data }; return h.flush() },
    unmount() { slots.forEach(s => s?.cleanup?.()) },
    get wrapper() { return dom.get('root') },
    get svg() { return [...dom.values()].find(el => el.node.type === 'svg') },
    get view() { return [...dom.values()].find(el => el.node.type === 'g' && !el.node.props.onPointerDown) },
    get nodes() { return [...dom.values()].filter(el => el.node.type === 'g' && el.node.props.onPointerDown) },
  }
  return h.flush()
}

const dataOf = () => ({ nodes: [{ id: 'a', label: '甲', category: '分类' }, { id: 'b', label: '乙', category: '分类' }], links: [{ source: 'a', target: 'b' }] })
const pointer = (el, x, y) => ({ currentTarget: el, target: el, pointerId: 1, clientX: x, clientY: y, stopPropagation() {} })

test('相同数据卸载重挂保留已收敛布局，不重新种子或启动模拟', () => {
  const env = environment()
  try {
    const data = dataOf(), h = mount(data)
    env.settle()
    const positions = h.nodes.map(el => el.attrs.transform)
    h.unmount()
    const returned = mount(data)
    try {
      assert.deepEqual(returned.nodes.map(el => el.attrs.transform), positions)
      assert.equal(env.frames.size, 0, '已收敛图谱重挂不应重新模拟')
    } finally { returned.unmount() }
  } finally { env.restore() }
})

test('手动缩放和平移后的视图跨卸载复用，resize仍保留用户视图', () => {
  const env = environment()
  try {
    const data = dataOf(), h = mount(data)
    env.settle()
    h.svg.listeners.get('wheel')({ deltaY: -200, clientX: 120, clientY: 80, preventDefault() {} })
    h.svg.node.props.onPointerDown(pointer(h.svg, 100, 100))
    h.svg.node.props.onPointerMove(pointer(h.svg, 140, 120))
    h.svg.node.props.onPointerUp(pointer(h.svg, 140, 120))
    const view = h.view.attrs.transform
    h.unmount()
    const returned = mount(data)
    try {
      assert.equal(returned.view.attrs.transform, view)
      returned.wrapper.clientWidth = 1200; env.resize()
      assert.equal(returned.view.attrs.transform, view)
    } finally { returned.unmount() }
  } finally { env.restore() }
})

test('自动适配视图在重挂后仍随resize变化，节点不重新排布', () => {
  const env = environment()
  try {
    const data = dataOf(), h = mount(data)
    env.settle(); h.unmount()
    const returned = mount(data)
    try {
      const view = returned.view.attrs.transform, positions = returned.nodes.map(el => el.attrs.transform)
      returned.wrapper.clientWidth = 1200; env.resize()
      assert.notEqual(returned.view.attrs.transform, view)
      assert.deepEqual(returned.nodes.map(el => el.attrs.transform), positions)
      assert.equal(env.frames.size, 0)
    } finally { returned.unmount() }
  } finally { env.restore() }
})

test('同一实例收到新数据重新建模，缩放事件绑定新视图，旧快照仍可复用', () => {
  const env = environment()
  try {
    const original = dataOf(), changed = dataOf(), h = mount(original)
    env.settle()
    const settled = h.nodes.map(el => el.attrs.transform)
    h.svg.listeners.get('wheel')({ deltaY: -300, clientX: 100, clientY: 80, preventDefault() {} })
    const oldView = h.view.attrs.transform
    h.update(changed)
    assert.notDeepEqual(h.nodes.map(el => el.attrs.transform), settled, '新快照需独立种子')
    assert.equal(env.frames.size, 1)
    assert.notEqual(h.view.attrs.transform, oldView)
    const newView = h.view.attrs.transform
    h.svg.listeners.get('wheel')({ deltaY: -100, clientX: 100, clientY: 80, preventDefault() {} })
    assert.notEqual(h.view.attrs.transform, newView)
    h.update(original)
    assert.deepEqual(h.nodes.map(el => el.attrs.transform), settled)
    assert.equal(h.view.attrs.transform, oldView, '新快照的wheel不能改写旧快照视图')
    assert.equal(env.frames.size, 0)
    h.unmount()
  } finally { env.restore() }
})

test('重挂后拖拽可重启已停止的模拟，拖拽中卸载释放固定点并保留最后位置', () => {
  const env = environment()
  try {
    const data = dataOf(), first = mount(data)
    env.settle(); first.unmount()
    const h = mount(data), node = h.nodes[0]
    node.node.props.onPointerDown(pointer(node, 400, 280))
    h.svg.node.props.onPointerMove(pointer(h.svg, 520, 340)); h.flush()
    assert.equal(env.frames.size, 1)
    assert.equal(h.nodes[0].attrs.transform, 'translate(520,340)')
    h.unmount()
    assert.equal(env.frames.size, 0, '卸载停止当前动画帧')
    const returned = mount(data)
    try {
      assert.equal(returned.nodes[0].attrs.transform, 'translate(520,340)')
      env.settle()
      assert.notEqual(returned.nodes[0].attrs.transform, 'translate(520,340)', '固定点应释放，布局可继续收敛')
    } finally { returned.unmount() }
  } finally { env.restore() }
})

test('未收敛时重挂延续冷却进度，不从头模拟；节点点击仍可导航', () => {
  const env = environment()
  try {
    const data = dataOf(), selected = [], h = mount(data)
    for (let i = 0; i < 100; i++) env.step()
    const positions = h.nodes.map(el => el.attrs.transform)
    h.unmount()
    const returned = mount(data, (...args) => selected.push(args))
    try {
      assert.deepEqual(returned.nodes.map(el => el.attrs.transform), positions)
      let steps = 0
      while (env.frames.size && steps < 400) { env.step(); steps++ }
      assert.ok(steps > 0 && steps < 220, `应延续已有冷却进度，实际${steps}帧`)
      const node = returned.nodes[0]
      node.node.props.onPointerDown(pointer(node, 400, 280))
      node.node.props.onPointerUp(pointer(node, 400, 280)); returned.flush()
      assert.deepEqual(selected, [['a', '甲']])
      assert.equal(env.frames.size, 1)
    } finally { returned.unmount() }
  } finally { env.restore() }
})
