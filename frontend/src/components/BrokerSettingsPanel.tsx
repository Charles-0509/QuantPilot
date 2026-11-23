import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, PlugZap, ShieldCheck } from 'lucide-react'
import { api } from '../api'
import { Badge, Button, Card, Field } from './UI'
import MarketDataSourcePanel from './MarketDataSourcePanel'

export type BrokerConfig = {
  broker: 'alpaca' | 'ibkr'
  trading_mode: 'paper' | 'live'
  ibkr_gateway_url: string
  ibkr_account_id: string
  ibkr_verify_tls: boolean
  revision: number
  market_data_source?: 'alpaca' | 'ibkr'
}
const defaults: BrokerConfig = { broker: 'alpaca', trading_mode: 'paper', ibkr_gateway_url: 'https://127.0.0.1:5000/v1/api', ibkr_account_id: '', ibkr_verify_tls: true, revision: 0 }
type Challenge = { challenge_token: string; disclaimer: string; account_id: string; expires_at: string }

export default function BrokerSettingsPanel() {
  const client = useQueryClient()
  const config = useQuery({ queryKey: ['broker-config'], queryFn: () => api<BrokerConfig>('/api/brokers/config') })
  const [target, setTarget] = useState<BrokerConfig>(defaults)
  const [accounts, setAccounts] = useState<Array<{ account_id: string; paper: boolean }>>([])
  const [challenge, setChallenge] = useState<Challenge | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const pendingTarget = useRef<BrokerConfig | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const dialog = useRef<HTMLElement>(null)
  useEffect(() => {
    if (config.data?.broker) setTarget(config.data)
  }, [config.data])
  useEffect(() => { if (modalOpen) input.current?.focus() }, [modalOpen, challenge])

  const resetConsent = () => {
    setConfirmation(''); setChallenge(null); setModalOpen(false); pendingTarget.current = null
  }
  const edit = (patch: Partial<BrokerConfig>) => {
    resetConsent(); setError(''); setNotice('')
    setTarget((current) => ({ ...current, ...patch, trading_mode: 'paper' }))
  }
  const refresh = () => {
    for (const key of ['broker-config', 'connection-config', 'connection', 'dashboard', 'engine', 'risk-settings', 'orders', 'positions', 'bars', 'quotes', 'signals']) client.invalidateQueries({ queryKey: [key] })
  }
  const save = useMutation({
    mutationFn: (body: BrokerConfig & { confirmation_text?: string; challenge_token?: string }) => api<BrokerConfig>('/api/brokers/config', { method: 'PUT', body: JSON.stringify(body) }),
    onSuccess: (saved) => {
      setTarget(saved); resetConsent(); setError('')
      setNotice(saved.trading_mode === 'live' ? '已确认 IBKR 真实账户。引擎保持暂停，请核对策略和风控后手动启动。' : '已保存模拟环境连接。引擎保持暂停；现金归集已关闭。')
      refresh()
    },
    onError: (err: Error) => { setError(err.message); resetConsent() },
  })
  const probe = useMutation({
    mutationFn: () => api<{ accounts: Array<{ account_id: string; paper: boolean }> }>('/api/brokers/ibkr/probe', { method: 'POST', body: JSON.stringify(target) }),
    onSuccess: (result) => {
      setAccounts(result.accounts)
      const paper = result.accounts.filter((a) => a.paper)
      if (!target.ibkr_account_id && paper.length === 1) edit({ ibkr_account_id: paper[0].account_id })
      setError(''); setNotice('已读取网关账户；模拟/真实环境由IBKR返回值核验。')
    },
    onError: (err: Error) => setError(err.message),
  })
  const requestLive = useMutation({
    mutationFn: (body: BrokerConfig) => api<Challenge>('/api/brokers/ibkr/live-challenge', { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: (result, requested) => {
      if (pendingTarget.current && JSON.stringify(pendingTarget.current) === JSON.stringify(requested)) setChallenge(result)
    },
    onError: (err: Error) => { setError(err.message); resetConsent() },
  })
  const openLive = () => {
    setError(''); setNotice(''); setConfirmation(''); setChallenge(null)
    if (!target.ibkr_account_id.trim()) { setError('请先选择或填写 IBKR 账户ID'); return }
    const next = { ...target, broker: 'ibkr' as const, trading_mode: 'live' as const }
    pendingTarget.current = next
    setModalOpen(true)
    requestLive.mutate(next)
  }
  const submit = () => {
    if (target.broker === 'ibkr' && target.trading_mode === 'live') openLive()
    else save.mutate({ ...target, trading_mode: 'paper' })
  }
  const live = config.data?.broker === 'ibkr' && config.data.trading_mode === 'live'

  return <>
  <Card style={{ marginBottom: 16 }}>
    <div className="card-header"><div><h2>券商与交易账户</h2><p>Alpaca Paper / IBKR；新连接默认模拟环境</p></div><Badge tone={live ? 'danger' : 'info'}>{live ? 'IBKR 真实账户' : '模拟环境'}</Badge></div>
    <div className="form-section">
      <Field label="选择券商"><select aria-label="选择券商" value={target.broker} onChange={(e) => edit({ broker: e.target.value as BrokerConfig['broker'] })}><option value="alpaca">Alpaca Paper</option><option value="ibkr">Interactive Brokers（IBKR）</option></select></Field>
      {target.broker === 'ibkr' && <>
        <Field label="IBKR 网关地址" hint="先在 Client Portal Gateway 的浏览器页面完成登录和双重认证。地址须可由服务器访问。"><input aria-label="IBKR 网关地址" value={target.ibkr_gateway_url} onChange={(e) => edit({ ibkr_gateway_url: e.target.value })} /></Field>
        <Field label="IBKR 账户ID" hint="点击读取网关账户后选择；账户环境会由IBKR核验。"><input aria-label="IBKR 账户ID" value={target.ibkr_account_id} onChange={(e) => edit({ ibkr_account_id: e.target.value.toUpperCase() })} list="ibkr-accounts" /><datalist id="ibkr-accounts">{accounts.map((a) => <option key={a.account_id} value={a.account_id}>{a.paper ? 'Paper' : '真实账户'}</option>)}</datalist></Field>
        <label className="toggle-row"><span>验证网关 TLS 证书<p className="field-hint">默认开启。仅对你信任的本机/内网自签名网关关闭。</p></span><input type="checkbox" aria-label="验证网关 TLS 证书" checked={target.ibkr_verify_tls} onChange={(e) => edit({ ibkr_verify_tls: e.target.checked })} /></label>
        <Button variant="secondary" disabled={probe.isPending || save.isPending} onClick={() => probe.mutate()}><PlugZap size={15} />{probe.isPending ? '正在读取账户...' : '读取网关账户'}</Button>
        <label className="toggle-row"><span><strong>启用 IBKR 真实账户</strong><p className="field-hint">每次从模拟环境切入真实账户，均需重新阅读风险并输入确认。</p></span><input type="checkbox" aria-label="启用 IBKR 真实账户" checked={target.trading_mode === 'live'} disabled={save.isPending || requestLive.isPending} onChange={(e) => {
          if (e.target.checked) openLive()
          else save.mutate({ ...target, trading_mode: 'paper', ibkr_account_id: '' })
        }} /></label>
        <p className="field-hint">当前IBKR接入使用REST轮询，支持美元美股/ETF、市价、限价和Bracket。移动止损与自动现金归集暂不启用。网关会话失效后需重新登录。</p>
      </>}
      {target.broker === 'alpaca' && <p className="field-hint">保存为当前券商后，在下方填写或更新 Alpaca Paper 密钥。</p>}
      {(error || config.error) && <div role="alert" className="form-error">{error || '无法读取券商配置，请刷新重试'}</div>}
      {notice && <div role="status" className="success-callout">{notice}</div>}
      <Button disabled={save.isPending || requestLive.isPending || Boolean(config.error)} onClick={submit}><ShieldCheck size={15} />{save.isPending ? '正在验证连接...' : '保存券商与账户'}</Button>
    </div>
    {modalOpen && <div className="modal-backdrop" onKeyDown={(e) => {
      if (e.key === 'Escape' && !save.isPending) resetConsent()
      if (e.key === 'Tab') {
        const items = dialog.current?.querySelectorAll<HTMLElement>('input:not(:disabled), button:not(:disabled)')
        if (!items?.length) return
        const first = items[0], last = items[items.length - 1]
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
      }
    }}>
      <section ref={dialog} className="risk-modal" role="dialog" aria-modal="true" aria-labelledby="live-risk-title">
        <h2 id="live-risk-title"><AlertTriangle size={21} />启用 IBKR 真实交易账户</h2>
        <p>目标账户：<strong>{pendingTarget.current?.ibkr_account_id}</strong></p>
        <div className="danger-callout">{challenge?.disclaimer || '正在加载风险说明...'}</div>
        <Field label="请输入“我同意并确认”"><input ref={input} aria-label="请输入我同意并确认" autoComplete="off" value={confirmation} onChange={(e) => setConfirmation(e.target.value)} /></Field>
        <p className="field-hint">本次确认仅用于上述账户和当前连接配置，切换模拟账户后不能复用。</p>
        <div className="settings-actions"><Button variant="ghost" disabled={save.isPending} onClick={resetConsent}>取消</Button><Button variant="danger" disabled={confirmation !== '我同意并确认' || !challenge || save.isPending} onClick={() => {
          if (pendingTarget.current && challenge) save.mutate({ ...pendingTarget.current, confirmation_text: confirmation, challenge_token: challenge.challenge_token })
        }}>{save.isPending ? '正在核验账户...' : '确认启用真实账户'}</Button></div>
      </section>
    </div>}
  </Card>
  {config.data && <MarketDataSourcePanel broker={config.data.broker} configuredSource={config.data.market_data_source || 'alpaca'} />}
  </>
}
