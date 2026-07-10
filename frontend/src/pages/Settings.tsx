import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Database, Eye, EyeOff, KeyRound, LockKeyhole, LogOut, Save, Server, ShieldCheck, Trash2, UserRound, WifiOff } from 'lucide-react'
import { api, ApiError, formatTime } from '../api'
import { Badge, Button, Card, Field, Loading, PageHeader } from '../components/UI'
import type { AuthUser, ConnectionConfig } from '../types'

type ConnectionStatus = {
  configured: boolean
  connected: boolean
  paper: boolean
  feed: string
  source: 'web' | 'env' | 'none'
  message: string
}

export default function SettingsPage() {
  const client = useQueryClient()
  const [apiKeyId, setApiKeyId] = useState('')
  const [apiSecretKey, setApiSecretKey] = useState('')
  const [showSecret, setShowSecret] = useState(false)
  const [formError, setFormError] = useState('')
  const [notice, setNotice] = useState('')
  const connection = useQuery({ queryKey: ['connection'], queryFn: () => api<ConnectionStatus>('/api/connection'), refetchInterval: 15000 })
  const config = useQuery({ queryKey: ['connection-config'], queryFn: () => api<ConnectionConfig>('/api/connection/config') })

  const refresh = () => {
    client.invalidateQueries({ queryKey: ['connection'] })
    client.invalidateQueries({ queryKey: ['connection-config'] })
    client.invalidateQueries({ queryKey: ['dashboard'] })
    client.invalidateQueries({ queryKey: ['engine'] })
  }
  const save = useMutation({
    mutationFn: () => api<ConnectionConfig>('/api/connection/config', {
      method: 'PUT',
      body: JSON.stringify({ api_key_id: apiKeyId, api_secret_key: apiSecretKey, data_feed: 'iex' }),
    }),
    onSuccess: () => {
      setApiKeyId('')
      setApiSecretKey('')
      setFormError('')
      setNotice('Alpaca Paper 配置已验证并保存。交易引擎已暂停，请在自动交易页面确认后重新启动。')
      refresh()
    },
    onError: (error: Error) => setFormError(error instanceof ApiError ? error.message : '保存连接配置失败，请稍后重试'),
  })
  const remove = useMutation({
    mutationFn: () => api<ConnectionConfig>('/api/connection/config', { method: 'DELETE' }),
    onSuccess: () => {
      setApiKeyId('')
      setApiSecretKey('')
      setFormError('')
      setNotice('当前账户的网页 Alpaca 配置已移除，交易引擎保持暂停。')
      refresh()
    },
    onError: () => setFormError('移除网页配置失败，请稍后重试'),
  })

  if (connection.isLoading || config.isLoading) return <Loading label="正在读取本机连接配置" />
  const status = connection.data
  const saved = config.data
  const sourceLabel = saved?.source === 'web' ? '网页加密配置' : saved?.source === 'env' ? '.env 后备配置' : '尚未配置'
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setNotice('')
    if (!apiKeyId.trim() || !apiSecretKey.trim()) {
      setFormError('请完整填写 Alpaca Paper API Key 与 Secret')
      return
    }
    setFormError('')
    save.mutate()
  }
  const removeWebConfig = () => {
    if (window.confirm('移除当前账户保存的 Alpaca Paper 密钥并暂停交易引擎？')) {
      setNotice('')
      remove.mutate()
    }
  }

  return <>
    <PageHeader
      eyebrow="SECURE SYSTEM CONFIG"
      title="设置"
      description="直接在此验证并更新 Alpaca Paper 密钥。程序没有实盘开关，也不会将密钥返回到浏览器。"
      actions={<Badge tone={status?.connected ? 'success' : 'warning'}>{status?.connected ? '模拟盘已连接' : '未连接'}</Badge>}
    />
    <div className="two-column">
      <Card>
        <div className="card-header"><div><h2>Alpaca Paper 连接</h2><p>保存前会连接 Alpaca Paper 验证账户</p></div><ShieldCheck size={19} color="#3df6de" /></div>
        <form className="form-section" onSubmit={submit}>
          <Field label="API Key ID" hint="填入 Alpaca Paper Account 生成的 Key ID；保存后只显示末四位。">
            <input aria-label="API Key ID" autoComplete="off" value={apiKeyId} onChange={(event) => setApiKeyId(event.target.value)} placeholder={saved?.api_key_hint ? `当前已保存 ${saved.api_key_hint}，输入新值以替换` : '输入 Alpaca Paper API Key ID'} />
          </Field>
          <Field label="API Secret Key" hint="仅用于本机验证和调用，保存后不会显示或发送回浏览器。">
            <div className="input-with-action">
              <input aria-label="API Secret Key" autoComplete="new-password" type={showSecret ? 'text' : 'password'} value={apiSecretKey} onChange={(event) => setApiSecretKey(event.target.value)} placeholder="输入 Alpaca Paper API Secret Key" />
              <button type="button" className="icon-button input-action" onClick={() => setShowSecret((value) => !value)} title={showSecret ? '隐藏 Secret' : '显示 Secret'} aria-label={showSecret ? '隐藏 Secret' : '显示 Secret'}>{showSecret ? <EyeOff size={16} /> : <Eye size={16} />}</button>
            </div>
          </Field>
          <Field label="行情数据源" hint="免费 IEX 数据源固定启用，当前版本不提供 SIP 或实盘选择。">
            <select aria-label="行情数据源" value="iex" disabled><option value="iex">IEX 免费实时行情</option></select>
          </Field>
          {formError && <div className="form-error" role="alert">{formError}</div>}
          {notice && <div className="success-callout" role="status">{notice}</div>}
          <div className="settings-actions">
            <Button type="submit" disabled={save.isPending || remove.isPending}><Save size={15} />{save.isPending ? '正在验证 Alpaca Paper...' : '验证并保存连接'}</Button>
            {saved?.source === 'web' && <Button type="button" variant="danger" disabled={save.isPending || remove.isPending} onClick={removeWebConfig}><Trash2 size={15} />{remove.isPending ? '正在移除...' : '移除网页配置'}</Button>}
          </div>
        </form>
      </Card>
      <Card>
        <div className="card-header"><div><h2>当前连接状态</h2><p>固定为 Alpaca Paper Trading</p></div>{status?.connected ? <CheckCircle2 size={19} color="#42e6a4" /> : <WifiOff size={19} color="#f2bd5c" />}</div>
        <div className="card-pad stack">
          <StatusRow icon={<KeyRound size={16} />} label="配置来源" value={sourceLabel} />
          <StatusRow icon={<KeyRound size={16} />} label="API 密钥" value={saved?.configured ? (saved.api_key_hint || '已保存') : '尚未配置'} />
          <StatusRow icon={<LockKeyhole size={16} />} label="交易环境" value="Paper Trading（硬编码）" />
          <StatusRow icon={<Server size={16} />} label="行情数据源" value="IEX 免费实时数据" />
          <StatusRow icon={<Database size={16} />} label="最后更新" value={saved?.updated_at ? formatTime(saved.updated_at) : '—'} />
          <div className="warning-callout">{status?.message}</div>
        </div>
      </Card>
    </div>
    <Card style={{ marginTop: 16 } as any}>
      <div className="card-header"><div><h2>服务器存储与系统边界</h2><p>连接凭据只在当前 QuantPilot 实例的 Docker 数据卷内使用</p></div></div>
      <div className="settings-security-grid">
        <SecurityItem label="加密保存" value="密文存入 SQLite，解密密钥保存在服务器 data/.credentials.key" />
        <SecurityItem label="账户隔离" value="每个 QuantPilot 用户单独保存并使用自己的 Alpaca Paper 凭据" />
        <SecurityItem label="变更保护" value="保存或移除配置后，引擎会暂停且不会自动恢复" />
        <SecurityItem label="资产范围" value="美股 / ETF，只做多；常规交易时段" />
        <SecurityItem label="实时订阅" value="IEX 最多 30 个股票代码" />
        <SecurityItem label="部署要求" value="保护 data/ 目录，运行自动交易的主机需保持开机联网" />
      </div>
    </Card>
    <AccountSecurity />
  </>
}

function AccountSecurity() {
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const me = useQuery({ queryKey: ['auth-me'], queryFn: () => api<AuthUser>('/api/auth/me') })
  const changePassword = useMutation({
    mutationFn: () => api<void>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),
    onSuccess: () => window.location.reload(),
    onError: (requestError: Error) => setError(requestError instanceof ApiError ? requestError.message : '修改密码失败'),
  })
  const logoutAll = useMutation({
    mutationFn: () => api<void>('/api/auth/logout-all', { method: 'POST' }),
    onSuccess: () => window.location.reload(),
    onError: () => setError('注销会话失败，请稍后重试'),
  })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (newPassword.length < 12) {
      setError('新密码至少需要12位')
      return
    }
    if (newPassword !== confirmPassword) {
      setError('两次输入的新密码不一致')
      return
    }
    changePassword.mutate()
  }
  const revokeAll = () => {
    if (window.confirm('注销包括当前浏览器在内的全部登录会话？')) logoutAll.mutate()
  }

  return <Card style={{ marginTop: 16 } as any}>
    <div className="card-header"><div><h2>账户与会话安全</h2><p>OAuth2 不透明会话令牌仅以摘要形式保存</p></div><UserRound size={19} color="#a775ff" /></div>
    <div className="two-column card-pad">
      <div className="stack">
        <StatusRow icon={<UserRound size={16} />} label="当前账户" value={me.data ? `${me.data.username} · ${me.data.role === 'admin' ? '管理员' : '普通用户'}` : '—'} />
        <StatusRow icon={<LockKeyhole size={16} />} label="会话有效期" value="12小时" />
        <div className="warning-callout">修改密码后会注销全部设备，需要使用新密码重新登录。</div>
        <Button type="button" variant="danger" onClick={revokeAll} disabled={logoutAll.isPending}><LogOut size={15} />{logoutAll.isPending ? '正在注销...' : '退出所有设备'}</Button>
      </div>
      <form className="stack" onSubmit={submit}>
        <Field label="当前密码"><input aria-label="当前密码" type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></Field>
        <Field label="新密码" hint="长度至少12位"><input aria-label="新密码" type="password" autoComplete="new-password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></Field>
        <Field label="确认新密码"><input aria-label="确认新密码" type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} /></Field>
        {error && <div className="form-error" role="alert">{error}</div>}
        <Button type="submit" disabled={changePassword.isPending}><KeyRound size={15} />{changePassword.isPending ? '正在更新...' : '修改密码并注销会话'}</Button>
      </form>
    </div>
  </Card>
}

function StatusRow({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return <div className="toggle-row"><div style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#8293aa' }}>{icon}<span>{label}</span></div><strong className="status-row-value">{value}</strong></div>
}

function SecurityItem({ label, value }: { label: string; value: string }) {
  return <div className="metric-item"><span>{label}</span><strong>{value}</strong></div>
}
