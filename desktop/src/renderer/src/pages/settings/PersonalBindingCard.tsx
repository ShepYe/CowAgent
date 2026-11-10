import React, { useEffect, useState } from 'react'
import apiClient, { type PersonalBinding } from '../../api/client'

const labels: Record<keyof PersonalBinding, string> = {
  channel_instance_id: '渠道实例 ID', app_id: 'App ID', tenant_id: 'Tenant ID', sender_open_id: '本人 Open ID',
}

export default function PersonalBindingCard() {
  const [binding, setBinding] = useState<PersonalBinding>({ channel_instance_id: '', app_id: '', tenant_id: '', sender_open_id: '' })
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    apiClient.personalBinding().then(res => {
      if (res.status === 'success') setBinding(previous => ({ ...previous, ...res.binding }))
      else setMessage(res.message || '读取绑定失败')
    }).catch(e => setMessage(e.message))
  }, [])
  const save = async () => {
    setBusy(true); setMessage('')
    try {
      const res = await apiClient.personalBinding(binding)
      setMessage(res.status === 'success' ? '本人绑定已保存' : res.message || '保存失败')
    } catch (e) { setMessage(e instanceof Error ? e.message : '保存失败') }
    finally { setBusy(false) }
  }
  return <section className="rounded-lg border border-default p-5 space-y-3">
    <h3 className="font-semibold">本人飞书绑定 <span title="仅桌面认证入口可修改。四个标识共同确定本人身份，只接受该身份的私聊。">?</span></h3>
    <div className="grid grid-cols-2 gap-3">{(Object.keys(labels) as (keyof PersonalBinding)[]).map(key => <label className="text-sm space-y-1" key={key}><span>{labels[key]}</span><input className="w-full bg-inset border border-default rounded-btn px-3 py-2" value={binding[key]} disabled={busy} onChange={e => setBinding({ ...binding, [key]: e.target.value })} /></label>)}</div>
    <button className="px-3 py-2 rounded-btn border border-default disabled:opacity-50" disabled={busy || Object.values(binding).some(v => !v.trim())} onClick={() => void save()}>{busy ? '保存中…' : '保存绑定'}</button>
    {message && <div role="status" className="text-sm">{message}</div>}
  </section>
}
