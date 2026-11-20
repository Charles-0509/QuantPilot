import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Sparkles } from 'lucide-react'
import { api } from '../api'
import type { AiProvider, AiSettings } from '../types'
import { Badge, Button, Card, ErrorPanel, Field } from './UI'

const PRESETS: Array<{ label: string; provider: AiProvider; baseUrl: string; model: string }> = [
  { label: 'OpenAI（Responses）', provider: 'openai_responses', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4.1-mini' },
  { label: 'OpenAI（Chat Completions）', provider: 'openai', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4.1-mini' },
  { label: 'Anthropic (Claude)', provider: 'anthropic', baseUrl: 'https://api.anthropic.com', model: 'claude-sonnet-4-5' },
  { label: 'DeepSeek', provider: 'openai', baseUrl: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  { label: '通义千问', provider: 'openai', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
  { label: 'Google Gemini', provider: 'openai', baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai', model: 'gemini-2.5-flash' },
  { label: 'OpenRouter', provider: 'openai', baseUrl: 'https://openrouter.ai/api/v1', model: 'openai/gpt-4.1-mini' },
  { label: '本地 Ollama', provider: 'openai', baseUrl: 'http://127.0.0.1:11434/v1', model: 'qwen2.5' },
]

export default function AiSettingsPanel() {
  const client = useQueryClient()
  const settings = useQuery({ queryKey: ['ai-settings'], queryFn: () => api<AiSettings>('/api/ai/settings') })
  const [provider, setProvider] = useState<AiProvider>('openai')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  useEffect(() => {
    if (settings.data?.configured) { setProvider(settings.data.provider); setBaseUrl(settings.data.base_url); setModel(settings.data.model) }
  }, [settings.data])
  const body = () => JSON.stringify({ provider, base_url: baseUrl, model, api_key: apiKey })
  const done = () => { setApiKey(''); client.invalidateQueries({ queryKey: ['ai-settings'] }) }
  const save = useMutation({ mutationFn: () => api<AiSettings>('/api/ai/settings', { method: 'PUT', body: body() }), onSuccess: done })
  const remove = useMutation({ mutationFn: () => api<AiSettings>('/api/ai/settings', { method: 'DELETE' }), onSuccess: done })
  const test = useMutation({ mutationFn: () => api<{ ok: boolean; reply: string }>('/api/ai/test', { method: 'POST', body: body() }) })
  const ready = Boolean(baseUrl && model && (apiKey || settings.data?.configured))
  const error = save.error || remove.error || test.error

  return <Card style={{ marginTop: 16 }}>
    <div className="form-section">
      <div className="form-section-title"><div><h3><Sparkles size={16} style={{ display: 'inline', verticalAlign: 'middle', marginRight: 6 }} />AI 策略助手</h3>
        <p>用于“自然语言写策略”。选择接口类型：OpenAI 官方两种接口都支持（Responses 为新接口，Chat Completions 为通用接口）；DeepSeek、通义千问、Gemini、OpenRouter、Ollama、new-api 网关通常只兼容 Chat Completions；Claude 官方接口选 Anthropic。API Key 加密保存在服务器，不会回传网页。</p></div>
        <Badge tone={settings.data?.configured ? 'success' : 'neutral'}>{settings.data?.configured ? '已配置' : '未配置'}</Badge></div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
        <span className="field-hint" style={{ alignSelf: 'center' }}>快速填入：</span>
        {PRESETS.map((preset) => <button key={preset.label} className="badge badge-neutral" style={{ cursor: 'pointer', border: 'none' }}
          onClick={() => { setProvider(preset.provider); setBaseUrl(preset.baseUrl); setModel(preset.model); test.reset() }}>{preset.label}</button>)}
      </div>
      <div className="form-grid">
        <Field label="接口类型"><select value={provider} onChange={(event) => setProvider(event.target.value as AiProvider)}>
          <option value="openai">OpenAI Chat Completions 兼容（/chat/completions）</option>
          <option value="openai_responses">OpenAI Responses（/responses）</option>
          <option value="anthropic">Anthropic（/v1/messages）</option>
        </select></Field>
        <Field label="接口地址" hint={provider === 'anthropic' ? '例如 https://api.anthropic.com' : '通常以 /v1 结尾'}>
          <input value={baseUrl} placeholder={provider === 'anthropic' ? 'https://api.anthropic.com' : 'https://api.openai.com/v1'} onChange={(event) => setBaseUrl(event.target.value.trim())} /></Field>
        <Field label="模型名称" hint="以服务商控制台显示的名称为准"><input value={model} onChange={(event) => setModel(event.target.value.trim())} /></Field>
        <Field label={settings.data?.configured ? 'API Key（留空则沿用已保存的）' : 'API Key'}><input type="password" value={apiKey} autoComplete="off" onChange={(event) => setApiKey(event.target.value)} /></Field>
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
        <Button variant="secondary" disabled={!ready || test.isPending} onClick={() => test.mutate()}>{test.isPending ? '正在测试…' : '测试连接'}</Button>
        <Button disabled={save.isPending || !baseUrl || !model || (!settings.data?.configured && !apiKey)} onClick={() => save.mutate()}>保存 AI 配置</Button>
        {settings.data?.configured && <Button variant="ghost" disabled={remove.isPending} onClick={() => remove.mutate()}>删除配置</Button>}
      </div>
      {test.data && <div className="warning-callout" style={{ marginTop: 12, color: '#42e6a4' }}>连接成功，模型回复：{test.data.reply}</div>}
      {save.isSuccess && <div className="warning-callout" style={{ marginTop: 12, color: '#42e6a4' }}>AI 配置已保存。</div>}
      {error && <div style={{ marginTop: 12 }}><ErrorPanel message={(error as Error).message} /></div>}
    </div>
  </Card>
}
