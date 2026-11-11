import React, { useEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import apiClient, { type PersonalBinding, type PersonalFeishuPairing, type PersonalFeishuPairingAction, type PersonalFeishuPairingResponse } from '../../api/client'
import { FieldTip } from './primitives'

const labels: Record<keyof PersonalBinding, string> = {
  channel_instance_id: '渠道实例 ID', app_id: 'App ID', tenant_id: '租户标识', sender_open_id: '本人 Open ID',
}
const tips: Record<keyof PersonalBinding, string> = {
  channel_instance_id: '取渠道卡底部的实例 ID，默认单实例为 feishu。',
  app_id: '取已配置的当前飞书应用 App ID。',
  tenant_id: '取该应用消息事件中的 sender.tenant_key。',
  sender_open_id: '取当前应用下本人用户的 Open ID（sender.sender_id.open_id），不是机器人 ID。',
}
const emptyBinding: PersonalBinding = { channel_instance_id: '', app_id: '', tenant_id: '', sender_open_id: '' }
const button = 'px-3 py-2 rounded-btn border border-default text-sm disabled:opacity-50'
const savedMessage = '本人绑定已保存，可以在飞书发送消息验证'
const manualSavedMessage = '本人绑定已保存，请回渠道重新连接'

export default function PersonalBindingCard() {
  const [binding, setBinding] = useState<PersonalBinding>(emptyBinding)
  const [pairing, setPairing] = useState<PersonalFeishuPairing>({ state: 'idle' })
  const [channels, setChannels] = useState<{ id: string; label: string }[]>([])
  const [selected, setSelected] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState(false)
  const [busy, setBusy] = useState(true)
  const [copied, setCopied] = useState(false)
  const section = useRef<HTMLElement>(null)
  const location = useLocation()
  const active = pairing.state === 'waiting' || pairing.state === 'pending'
  const notice = (text: string, failed = false) => { setMessage(text); setError(failed) }
  const apply = (res: PersonalFeishuPairingResponse) => {
    if (res.status !== 'success') throw new Error(res.message || '绑定请求失败')
    setPairing(res.pairing)
    setBinding({ ...emptyBinding, ...res.binding })
    if (res.pairing.state === 'confirmed') notice(savedMessage)
    else if (res.pairing.connection_error) notice(res.pairing.connection_error, true)
    else notice('')
  }

  useEffect(() => {
    if (location.hash === '#personal-feishu-binding') section.current?.scrollIntoView({ block: 'start' })
  }, [location.hash])

  useEffect(() => {
    let cancelled = false
    void Promise.allSettled([apiClient.personalFeishuPairing(), apiClient.getChannelsFull()]).then(([current, available]) => {
      if (cancelled) return
      const problems: string[] = []
      let preferred = ''
      if (current.status === 'fulfilled') {
        try {
          apply(current.value)
          if (current.value.status === 'success') preferred = current.value.pairing.channel_instance_id || current.value.binding.channel_instance_id
        } catch (e) { problems.push(e instanceof Error ? e.message : '读取绑定失败') }
      } else problems.push('读取绑定失败，请重新打开此页')
      if (available.status === 'fulfilled' && available.value.status === 'success') {
        const data = available.value
        const entries = data.multi_agent && data.multi_instance_types?.includes('feishu') ? data.instances || [] : data.channels
        const options = entries.filter(ch => ch.name === 'feishu' && ch.active && ch.fields.some(f => f.key === 'feishu_app_id' && f.value))
          .map(ch => ({ id: ch.instance_id || 'feishu', label: ch.instance_name ? `${ch.instance_name} · ${ch.instance_id || 'feishu'}` : ch.instance_id || 'feishu' }))
        setChannels(options)
        setSelected(options.find(ch => ch.id === preferred)?.id || options[0]?.id || '')
      } else problems.push('读取飞书渠道失败，请重新打开此页')
      if (problems.length) notice(problems.join('；'), true)
      setBusy(false)
    })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!active || busy) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const res = await apiClient.personalFeishuPairing()
        if (cancelled) return
        apply(res)
        if (res.status === 'success' && res.pairing.state !== 'waiting' && res.pairing.state !== 'pending') return
      } catch (e) {
        if (cancelled) return
        notice(e instanceof Error ? e.message : '读取绑定状态失败', true)
      }
      if (!cancelled) timer = setTimeout(() => void poll(), 2000)
    }
    timer = setTimeout(() => void poll(), 2000)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [active, busy, pairing.pairing_id])

  const act = async (body: PersonalFeishuPairingAction) => {
    setBusy(true); notice(''); setCopied(false)
    try { apply(await apiClient.personalFeishuPairing(body)) }
    catch (e) { notice(e instanceof Error ? e.message : '绑定请求失败', true) }
    finally { setBusy(false) }
  }
  const copy = async () => {
    if (!pairing.command) return
    try { await navigator.clipboard.writeText(pairing.command); setCopied(true) }
    catch { notice('复制失败，请手动复制绑定命令', true) }
  }
  const save = async () => {
    setBusy(true); notice('')
    try {
      const res = await apiClient.personalBinding(binding)
      notice(res.status === 'success' ? manualSavedMessage : res.message || '保存失败', res.status !== 'success')
    } catch (e) { notice(e instanceof Error ? e.message : '保存失败', true) }
    finally { setBusy(false) }
  }
  return <section ref={section} id="personal-feishu-binding" className="rounded-lg border border-default p-5 space-y-3 scroll-mt-6">
    <h3 className="font-semibold flex items-center gap-2">本人飞书绑定 <FieldTip tip="选择已配置的飞书渠道，复制绑定命令，用本人账号私聊发送给该应用机器人。收到验证消息后，还需在此确认为本人，才会保存身份。绑定期间仅接收验证，不处理普通消息。" /></h3>
    <div className="flex flex-wrap items-center gap-3">
      <select aria-label="飞书渠道" className="bg-inset border border-default rounded-btn px-3 py-2 text-sm" value={selected} disabled={busy || active} onChange={e => setSelected(e.target.value)}>
        {!channels.length && <option value="">暂无已配置的飞书渠道</option>}
        {channels.map(ch => <option key={ch.id} value={ch.id}>{ch.label}</option>)}
      </select>
      {!active && <button className={button} disabled={busy || !selected} onClick={() => void act({ action: 'start', channel_instance_id: selected })}>{busy ? '处理中…' : '开始绑定'}</button>}
      {!channels.length && !busy && <Link className="text-sm text-accent hover:underline" to="/channels">配置飞书渠道</Link>}
      {active && pairing.pairing_id && <button className={button} disabled={busy} onClick={() => void act({ action: 'cancel', pairing_id: pairing.pairing_id! })}>取消绑定</button>}
    </div>
    {pairing.state === 'waiting' && <div className="space-y-3">
      {pairing.command && <div className="flex flex-wrap items-center gap-3">
        <code className="bg-inset rounded-btn px-3 py-2 select-all">{pairing.command}</code>
        <button className={button} onClick={() => void copy()}>{copied ? '已复制' : '复制命令'}</button>
      </div>}
      <div className="text-sm text-content-secondary flex items-center gap-2" role="status">等待本人私聊验证 <FieldTip tip="用本人飞书账号私聊所选渠道的机器人，发送上方完整命令；收到验证后回到此处确认。" />{pairing.expires_in != null && <span className="text-content-tertiary">{Math.max(0, Math.ceil(pairing.expires_in))} 秒后过期</span>}</div>
    </div>}
    {pairing.state === 'pending' && <div className="space-y-3">
      <div role="status" className="text-sm">已收到验证消息</div>
      {pairing.candidate && <details className="text-sm"><summary className="cursor-pointer text-content-secondary">查看身份标识</summary><dl className="mt-2 space-y-1">{(Object.keys(labels) as (keyof PersonalBinding)[]).map(key => <div key={key}><dt className="text-content-tertiary">{labels[key]}</dt><dd className="font-mono break-all select-all">{pairing.candidate![key]}</dd></div>)}</dl></details>}
      <button className={`${button} text-accent`} disabled={busy || !pairing.pairing_id || !pairing.candidate} onClick={() => void act({ action: 'confirm', pairing_id: pairing.pairing_id! })}>确认为本人</button>
      <FieldTip tip="请确认刚才的私聊命令由你本人发送；点击后才保存该身份。" />
    </div>}
    {pairing.state === 'expired' && <div role="status" className="text-sm text-warning">绑定码已过期，请重新开始绑定</div>}
    {pairing.state === 'idle' && binding.sender_open_id && <div className="text-sm text-content-secondary">已绑定本人飞书</div>}
    {message && <div role={error ? 'alert' : 'status'} className={`text-sm ${error ? 'text-danger' : 'text-content-secondary'}`}>{message}</div>}
    {(pairing.state === 'confirmed' || message === manualSavedMessage) && <Link className="inline-block text-sm text-accent hover:underline" to="/channels">{message === manualSavedMessage ? '回渠道重新连接' : '返回渠道'}</Link>}
    <details className="border-t border-default pt-3">
      <summary className="text-sm text-content-secondary cursor-pointer">高级：手工绑定（备用）</summary>
      <div className="space-y-3 mt-3">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">{(Object.keys(labels) as (keyof PersonalBinding)[]).map(key => <label className="text-sm space-y-1" key={key}><span className="flex items-center gap-1">{labels[key]} <FieldTip tip={tips[key]} /></span><input className="w-full bg-inset border border-default rounded-btn px-3 py-2" value={binding[key]} disabled={busy || active} onChange={e => setBinding({ ...binding, [key]: e.target.value })} /></label>)}</div>
        <button className={button} disabled={busy || active || Object.values(binding).some(v => !v.trim())} onClick={() => void save()}>保存绑定</button>
      </div>
    </details>
  </section>
}
