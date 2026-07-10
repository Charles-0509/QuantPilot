import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, ShieldCheck, UserCog, UserPlus } from 'lucide-react'
import { api, ApiError, formatTime } from '../api'
import { Badge, Button, Card, ErrorPanel, Field, Loading, PageHeader } from '../components/UI'
import type { AuthUser } from '../types'

export default function Users() {
  const client = useQueryClient()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<'admin' | 'user'>('user')
  const [resetUser, setResetUser] = useState<AuthUser | null>(null)
  const [resetPassword, setResetPassword] = useState('')
  const [error, setError] = useState('')
  const me = useQuery({ queryKey: ['auth-me'], queryFn: () => api<AuthUser>('/api/auth/me') })
  const users = useQuery({
    queryKey: ['users'],
    queryFn: () => api<AuthUser[]>('/api/users'),
    enabled: me.data?.role === 'admin',
  })
  const refresh = () => client.invalidateQueries({ queryKey: ['users'] })
  const create = useMutation({
    mutationFn: () => api<AuthUser>('/api/users', {
      method: 'POST',
      body: JSON.stringify({ username, password, role }),
    }),
    onSuccess: () => {
      setUsername('')
      setPassword('')
      setRole('user')
      setError('')
      refresh()
    },
    onError: (requestError: Error) => setError(message(requestError, '创建用户失败')),
  })
  const update = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      api<AuthUser>(`/api/users/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
    onSuccess: () => { setError(''); refresh() },
    onError: (requestError: Error) => setError(message(requestError, '更新用户失败')),
  })
  const reset = useMutation({
    mutationFn: () => api<void>(`/api/users/${resetUser?.id}/reset-password`, {
      method: 'POST', body: JSON.stringify({ password: resetPassword }),
    }),
    onSuccess: () => { setResetUser(null); setResetPassword(''); setError(''); refresh() },
    onError: (requestError: Error) => setError(message(requestError, '重置密码失败')),
  })

  if (me.isLoading) return <Loading label="正在验证管理员权限" />
  if (me.data?.role !== 'admin') return <ErrorPanel message="仅管理员可以创建和管理 QuantPilot 用户。" />
  if (users.isLoading) return <Loading label="正在读取用户目录" />

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (password.length < 12) return setError('初始密码至少需要12位')
    create.mutate()
  }
  const submitReset = (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (resetPassword.length < 12) return setError('新密码至少需要12位')
    reset.mutate()
  }

  return <>
    <PageHeader
      eyebrow="TENANT ACCESS CONTROL"
      title="用户管理"
      description="为每位使用者创建独立账户。策略、回测、风控、运行日志和 Alpaca Paper 连接按用户隔离。"
      actions={<Badge tone="info"><ShieldCheck size={13} /> 管理员控制台</Badge>}
    />
    <div className="two-column users-layout">
      <Card>
        <div className="card-header"><div><h2>创建用户</h2><p>用户首次登录后需在设置页填写自己的 Alpaca Paper 密钥</p></div><UserPlus size={19} color="#3df6de" /></div>
        <form className="form-section" onSubmit={submit}>
          <Field label="用户名" hint="3至64位，可使用字母、数字、点、下划线和短横线。"><input aria-label="新用户名" autoComplete="off" value={username} onChange={(event) => setUsername(event.target.value)} /></Field>
          <Field label="初始密码" hint="至少12位；只保存 Argon2id 加盐哈希。"><input aria-label="初始密码" type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} /></Field>
          <Field label="角色"><select aria-label="用户角色" value={role} onChange={(event) => setRole(event.target.value as 'admin' | 'user')}><option value="user">普通用户</option><option value="admin">管理员</option></select></Field>
          {error && !resetUser && <div className="form-error" role="alert">{error}</div>}
          <Button type="submit" disabled={create.isPending}><UserPlus size={15} />{create.isPending ? '正在创建...' : '创建独立账户'}</Button>
        </form>
      </Card>
      <Card>
        <div className="card-header"><div><h2>账户目录</h2><p>{users.data?.length || 0} 个 QuantPilot 用户</p></div><UserCog size={19} color="#a775ff" /></div>
        <div className="table-scroll users-table-wrap"><table className="data-table"><thead><tr><th>用户</th><th>角色</th><th>Alpaca</th><th>状态</th><th>最近登录</th><th>操作</th></tr></thead><tbody>
          {users.data?.map((user) => <tr key={user.id}>
            <td className="symbol-cell">{user.username}{user.id === me.data?.id && <small className="current-user-tag">当前</small>}</td>
            <td><select aria-label={`${user.username}角色`} value={user.role} disabled={user.id === me.data?.id || update.isPending} onChange={(event) => update.mutate({ id: user.id, body: { role: event.target.value } })}><option value="user">普通用户</option><option value="admin">管理员</option></select></td>
            <td><Badge tone={user.alpaca_configured ? 'success' : 'warning'}>{user.alpaca_configured ? '已配置' : '待配置'}</Badge></td>
            <td><Badge tone={user.is_active ? 'success' : 'danger'}>{user.is_active ? '启用' : '停用'}</Badge></td>
            <td>{formatTime(user.last_login_at)}</td>
            <td><div className="row-actions"><Button variant="ghost" disabled={user.id === me.data?.id} onClick={() => { setResetUser(user); setResetPassword(''); setError('') }}><KeyRound size={14} />重置密码</Button><Button variant={user.is_active ? 'danger' : 'secondary'} disabled={user.id === me.data?.id || update.isPending} onClick={() => update.mutate({ id: user.id, body: { is_active: !user.is_active } })}>{user.is_active ? '停用' : '启用'}</Button></div></td>
          </tr>)}
        </tbody></table></div>
      </Card>
    </div>
    {resetUser && <div className="modal-backdrop" role="presentation" onMouseDown={() => setResetUser(null)}><section className="modal-panel" role="dialog" aria-modal="true" aria-labelledby="reset-title" onMouseDown={(event) => event.stopPropagation()}><div className="card-header"><div><h2 id="reset-title">重置 {resetUser.username} 的密码</h2><p>保存后会立即注销该用户的全部会话</p></div><KeyRound size={19} color="#f2bd5c" /></div><form className="form-section" onSubmit={submitReset}><Field label="新密码" hint="至少12位"><input aria-label="重置后的密码" type="password" autoFocus autoComplete="new-password" value={resetPassword} onChange={(event) => setResetPassword(event.target.value)} /></Field>{error && <div className="form-error" role="alert">{error}</div>}<div className="settings-actions"><Button type="submit" disabled={reset.isPending}>确认重置</Button><Button type="button" variant="ghost" onClick={() => setResetUser(null)}>取消</Button></div></form></section></div>}
  </>
}

function message(error: Error, fallback: string) {
  return error instanceof ApiError ? error.message : fallback
}
