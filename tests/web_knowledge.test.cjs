const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = readFileSync(path.join(__dirname, '../channel/web/static/js/views/knowledge.js'), 'utf8');
function fixture() {
  class Element {
    constructor() { this.children = []; this.style = {}; this.dataset = {}; this.attributes = {}; this.classList = { toggle() {} }; }
    appendChild(child) { this.children.push(child); }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(type, handler) { this['on' + type] = handler; }
    getBoundingClientRect() { return { left: 8, bottom: 60 }; }
    remove() { this.removed = true; }
  }
  const tree = new Element(), body = new Element(), opens = [];
  const document = { body, getElementById: () => tree, createElement: () => new Element(), querySelectorAll: () => [] };
  const sandbox = { document, window: { innerWidth: 320, innerHeight: 568 }, localStorage: { getItem: () => '' }, createDocEditor: () => ({}), escapeHtml: s => String(s).replaceAll('<', '&lt;').replaceAll('>', '&gt;') };
  vm.createContext(sandbox); vm.runInContext(source, sandbox);
  sandbox.openKnowledgeFile = (...args) => opens.push(args);
  return { sandbox, tree, body, opens };
}
const rows = [{ dir: '物理分类', files: [], children: [{ dir: '嵌套', files: [
  { name: '中文.md', title: '同名知识', category: '手动/有效分类', summary: '仅摘要出现星河预算 <原件>' },
  { name: '其他.md', title: '同名知识', summary: '其他用途' },
  { name: '旧页.md', title: '旧无摘要' },
], children: [] }] }];
const flatten = node => [node, ...node.children.flatMap(flatten)];
test('Web嵌套目录摘要独有词和有效分类命中，旧字段兼容，中文同名按完整路径打开', () => {
  const { sandbox: s, tree, opens } = fixture();
  assert.equal(s._hasFilterMatch(rows, '星河预算'), true);
  assert.equal(s._hasFilterMatch(rows, '手动/有效分类'), true);
  assert.equal(s._hasFilterMatch(rows, '历史独有词'), false);
  assert.equal(s._knowledgeFileMatches({ title: '旧无摘要', name: '旧页.md' }, '摘要'), true);
  s.renderKnowledgeTree(rows, [], '星河预算');
  const buttons = flatten(tree).filter(el => el.dataset.path);
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].dataset.path, '物理分类/嵌套/中文.md');
  assert.match(buttons[0].innerHTML, /&lt;原件&gt;/);
  buttons[0].onclick();
  assert.deepEqual(opens, [['物理分类/嵌套/中文.md', '同名知识']]);
});
test('Web窄窗口焦点及悬停可访问完整摘要，Escape关闭，缺摘要不生成占位', () => {
  const { sandbox: s, tree, body } = fixture();
  s.renderKnowledgeTree(rows, []);
  const button = flatten(tree).find(el => el.dataset.path === '物理分类/嵌套/中文.md');
  button.onfocus();
  const tip = body.children.at(-1);
  assert.equal(tip.textContent, rows[0].children[0].files[0].summary);
  assert.equal(tip.attributes.role, 'tooltip');
  button.onkeydown({ key: 'Escape' }); assert.equal(tip.removed, true);
  button.onmouseenter(); button.onmouseleave(); assert.equal(body.children.at(-1).removed, true);
  const old = flatten(tree).find(el => el.dataset.path?.endsWith('/旧页.md'));
  assert.equal(old.onfocus, undefined); assert.doesNotMatch(old.innerHTML, /knowledge-tree-summary/);
});
