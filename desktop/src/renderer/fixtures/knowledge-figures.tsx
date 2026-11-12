import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import Markdown from '../src/components/Markdown'
import Lightbox from '../src/components/Lightbox'
import apiClient from '../src/api/client'
import type { KnowledgeFigures } from '../src/types'
import fixture from './knowledge-figures-content.json'
import '../src/index.css'
const nativeFetch = window.fetch.bind(window)
let requests: string[] = []
const png = (old: boolean) => new Promise<Blob>(resolve => {
  const canvas = document.createElement('canvas'); canvas.width = 960; canvas.height = 520
  const ctx = canvas.getContext('2d')!; ctx.fillStyle = old ? '#ede3c3' : '#d6efea'; ctx.fillRect(0,0,960,520)
  ctx.fillStyle = '#183b45'; ctx.font = '42px sans-serif'; ctx.fillText(old ? 'HISTORY · OLD PORT 1 → 2' : 'CURRENT · NEW PORT 2 → 1',40,100)
  ctx.strokeStyle = '#276c64'; ctx.lineWidth = 12; ctx.strokeRect(100,180,760,200)
  canvas.toBlob(blob => resolve(blob!), 'image/png')
})
window.fetch = async (input, init) => {
  const url = new URL(String(input), location.href)
  if (!url.pathname.includes('/api/knowledge/figure')) return nativeFetch(input, init)
  requests.push(url.href); document.getElementById('requests')!.textContent = requests.join('\n')
  if (url.pathname.endsWith('figure-original')) return new Response('isolated original fixture')
  if (url.searchParams.get('figure_id') === 'broken') return new Response(JSON.stringify({code:'storage_failed'}),{status:422})
  await new Promise(resolve => setTimeout(resolve, url.searchParams.get('agent_id') === 'slow' ? 2500 : 80))
  return new Response(await png(url.searchParams.get('view') === 'history'), {headers:{'Content-Type':'image/png'}})
}
apiClient.setBaseUrl(location.origin)
apiClient.setAuthToken('fixture-auth-only')
function Fixture() {
  const [history, setHistory] = useState(false), [agent, setAgent] = useState('alpha')
  apiClient.setActiveAgentId(agent)
  const publish = history ? 'old-publication' : 'new-publication'
  const figures = {publish_id:publish, figure_state:'pending',figure_manifest:fixture.figure_manifest} as KnowledgeFigures
  return <main style={{maxWidth:920,margin:'auto',padding:20}}>
    <h1>知识图片隔离验证</h1><div style={{display:'flex',gap:12,flexWrap:'wrap',margin:'16px 0'}}>
      <button onClick={()=>setHistory(false)}>当前版本</button><button onClick={()=>setHistory(true)}>历史版本</button>
      <button onClick={()=>setAgent('slow')}>慢 Agent（2.5秒）</button><button onClick={()=>setAgent('beta')}>切换 Agent beta</button>
    </div><p data-testid="identity">{agent} · {publish} · {history ? 'history' : 'current'}</p>
    <Markdown content={fixture.content.split('fixture-publication').join(publish) + '\n\n普通 Markdown 图片：\n\n![普通](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aLTsAAAAASUVORK5CYII=)\n\n公式 $S_{21}$ 与 **中文强调**。'} figures={figures} figureView={history ? 'history' : 'current'} agentId={agent}/>
    <details><summary>请求记录</summary><pre id="requests" style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}/></details><Lightbox/>
  </main>
}
createRoot(document.getElementById('root')!).render(<Fixture/> )
