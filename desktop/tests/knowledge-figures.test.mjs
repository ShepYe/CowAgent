import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import vm from 'node:vm'
globalThis.localStorage = {getItem:()=>null,setItem(){}}
const require = createRequire(import.meta.url)
const fixture = JSON.parse(readFileSync(new URL('../src/renderer/fixtures/knowledge-figures-content.json', import.meta.url),'utf8'))
const output = await build({entryPoints:[new URL('../src/renderer/src/components/Markdown.tsx',import.meta.url).pathname.slice(process.platform === 'win32' ? 1 : 0)],bundle:true,write:false,format:'cjs',platform:'node',external:['react','react/jsx-runtime'],loader:{'.css':'empty'},plugins:[{name:'language',setup(b){b.onResolve({filter:/\/i18n$/},a=>({path:a.path,namespace:'stub'}));b.onLoad({filter:/.*/,namespace:'stub'},()=>({contents:'export const t=x=>x; export const getLang=()=>"zh"'}))}}]})
const module={exports:{}};new Function('require','module','exports',output.outputFiles[0].text)(require,module,module.exports)
const Markdown=module.exports.default
const figures={publish_id:'fixture-publication',figure_manifest:fixture.figure_manifest}
test('生产render_blocks正文：两张ready图注与pending图注各一次，懒加载前不请求PNG',()=>{
 const html=renderToStaticMarkup(React.createElement(Markdown,{content:fixture.content,figures,figureView:'history',agentId:'test-agent'}))
 for(const caption of fixture.figure_manifest.items.map(i=>i.caption)) assert.equal(html.split(caption).length-1,1)
 assert.equal((html.match(/data-figure-id=/g)||[]).length,2)
 assert.doesNotMatch(html,/<img/)
 assert.match(html,/view=history/);assert.match(html,/#page=3/);assert.match(html,/幻灯片5/);assert.match(html,/material=reading-copy/)
})
const sandbox={window:{markdownit:require('markdown-it')},document:{addEventListener(){}},escapeHtml:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),URLSearchParams,t:x=>x}
vm.createContext(sandbox);vm.runInContext(readFileSync(new URL('../../channel/web/static/js/core/markdown.js',import.meta.url),'utf8'),sandbox)
test('Web生产正文保留pending图注，受控URI使用占位且普通图片仍渲染',()=>{
 const html=sandbox.renderMarkdown(fixture.content+'\n\n![普通](https://example.test/plain.png)',{figures,figureView:'history',agentId:'test-agent'})
 assert.equal(html.split('待补图的已核验正文').length-1,1);assert.match(html,/data-figure-id="port"/);assert.match(html,/#page=3/);assert.match(html,/<img[^>]+plain.png/)
})
test('Web可见图队列并发最多4个，余下任务依次执行',async()=>{
 let active=0,max=0,done=0
 await Promise.all(Array.from({length:12},()=>new Promise(resolve=>sandbox.queueKnowledgeFigure(async()=>{active++;max=Math.max(max,active);await new Promise(r=>setTimeout(r,3));active--;done++;resolve()}))))
 assert.equal(max,4);assert.equal(done,12)
})
