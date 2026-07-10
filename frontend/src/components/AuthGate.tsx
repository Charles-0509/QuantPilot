import { useEffect, useState, type FormEvent, type PropsWithChildren } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Eye, EyeOff, LockKeyhole, Orbit, ShieldCheck } from 'lucide-react'
import { api, apiForm, ApiError } from '../api'
import type { AuthStatus, OAuthToken } from '../types'
import { Button, Field, Loading } from './UI'

export default function AuthGate({ children }: PropsWithChildren) {
  const client = useQueryClient()
  const auth = useQuery({
    queryKey: ['auth-status'],
    queryFn: () => api<AuthStatus>('/api/auth/status'),
    staleTime: 0,
    retry: false,
  })

  useEffect(() => {
    const handleUnauthorized = () => {
      client.removeQueries({ predicate: (query) => query.queryKey[0] !== 'auth-status' })
      client.setQueryData<AuthStatus>(['auth-status'], {
        setup_required: false,
        authenticated: false,
        user: null,
      })
    }
    window.addEventListener('quantpilot:unauthorized', handleUnauthorized)
    return () => window.removeEventListener('quantpilot:unauthorized', handleUnauthorized)
  }, [client])

  if (auth.isLoading) return <Loading label="正在验证 QuantPilot 会话" />
  if (auth.error) return <AuthUnavailable onRetry={() => auth.refetch()} />
  if (!auth.data?.authenticated) {
    return <AuthPage setup={Boolean(auth.data?.setup_required)} onSuccess={() => auth.refetch()} />
  }
  return children
}

function AuthPage({ setup, onSuccess }: { setup: boolean; onSuccess: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (!username.trim() || !password) {
      setError('请填写用户名和密码')
      return
    }
    if (setup && password !== confirmPassword) {
      setError('两次输入的密码不一致')
      return
    }
    if (setup && password.length < 12) {
      setError('管理员密码至少需要12位')
      return
    }
    setSubmitting(true)
    try {
      if (setup) {
        await api<OAuthToken>('/api/auth/setup', {
          method: 'POST',
          body: JSON.stringify({ username, password }),
        })
      } else {
        await apiForm<OAuthToken>('/api/auth/token', { username, password })
      }
      setPassword('')
      setConfirmPassword('')
      onSuccess()
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : '认证失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  return <div className="auth-shell">
    <div className="auth-ambient auth-ambient-one" />
    <div className="auth-ambient auth-ambient-two" />
    <section className="auth-panel">
      <div className="auth-brand"><div className="brand-mark"><Orbit size={28} /></div><div><strong>QUANTPILOT</strong><span>SECURE QUANT CONTROL</span></div></div>
      <div className="auth-status"><ShieldCheck size={15} /> OPAQUE OAUTH2 SESSION · PAPER ONLY</div>
      <p className="eyebrow">{setup ? 'FIRST RUN INITIALIZATION' : 'AUTHORIZED ACCESS'}</p>
      <h1>{setup ? '创建管理员' : '欢迎回来'}</h1>
      <p className="auth-description">{setup ? '这是首次启动。创建唯一管理员后，公开初始化入口将永久关闭。' : '登录后才能访问策略、账户、行情和自动交易控制。'}</p>
      <form className="auth-form" onSubmit={submit}>
        <Field label="管理员用户名" hint={setup ? '3至64位，可使用字母、数字、点、下划线和短横线。' : undefined}>
          <input aria-label="管理员用户名" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="输入管理员用户名" />
        </Field>
        <Field label="密码" hint={setup ? '至少12位；密码只以 Argon2id 加盐哈希保存。' : undefined}>
          <div className="input-with-action">
            <input aria-label="密码" type={showPassword ? 'text' : 'password'} autoComplete={setup ? 'new-password' : 'current-password'} value={password} onChange={(event) => setPassword(event.target.value)} placeholder="输入密码" />
            <button type="button" className="icon-button input-action" aria-label={showPassword ? '隐藏密码' : '显示密码'} title={showPassword ? '隐藏密码' : '显示密码'} onClick={() => setShowPassword((value) => !value)}>{showPassword ? <EyeOff size={16} /> : <Eye size={16} />}</button>
          </div>
        </Field>
        {setup && <Field label="确认密码"><input aria-label="确认密码" type={showPassword ? 'text' : 'password'} autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} placeholder="再次输入密码" /></Field>}
        {error && <div className="form-error" role="alert">{error}</div>}
        <Button type="submit" disabled={submitting}><LockKeyhole size={15} />{submitting ? '正在建立安全会话...' : setup ? '创建管理员并进入系统' : '登录 QuantPilot'}</Button>
      </form>
      <p className="auth-footnote">访问令牌不会写入浏览器存储；交易接口始终锁定为 Alpaca Paper。</p>
    </section>
  </div>
}

function AuthUnavailable({ onRetry }: { onRetry: () => void }) {
  return <div className="auth-shell"><section className="auth-panel"><div className="auth-brand"><div className="brand-mark"><Orbit size={28} /></div><div><strong>QUANTPILOT</strong><span>SECURE QUANT CONTROL</span></div></div><h1>认证服务暂不可用</h1><p className="auth-description">请检查服务器连接或稍后重试。</p><Button onClick={onRetry}>重新连接</Button></section></div>
}
