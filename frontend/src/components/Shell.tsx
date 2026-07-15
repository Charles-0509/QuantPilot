import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  BarChart3,
  BookOpenCheck,
  Bot,
  ChartCandlestick,
  FlaskConical,
  Gauge,
  LogOut,
  Orbit,
  Settings,
  ShieldCheck,
  Users,
} from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { api } from '../api'
import { connectionPresentation } from '../status'
import type { AuthUser, ConnectionStatus } from '../types'
import { Badge } from './UI'

const navigation = [
  { to: '/', label: '总览', icon: Gauge },
  { to: '/market', label: '行情中心', icon: ChartCandlestick },
  { to: '/strategies', label: '策略库', icon: BookOpenCheck },
  { to: '/backtests', label: '回测实验室', icon: FlaskConical },
  { to: '/trading', label: '自动交易', icon: Bot },
  { to: '/risk', label: '风险中心', icon: ShieldCheck },
  { to: '/settings', label: '设置', icon: Settings },
]

export default function Shell() {
  const client = useQueryClient()
  useEffect(() => {
    let socket: WebSocket | null = null
    let retryTimer: number | undefined
    let stopped = false
    let attempts = 0

    const connect = () => {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/events`)
      socket.onopen = () => { attempts = 0 }
      socket.onmessage = (message) => {
        try {
          const payload = JSON.parse(message.data) as { event?: string; data?: Record<string, unknown> }
          if (payload.event === 'backtest') {
            client.invalidateQueries({ queryKey: ['backtests'] })
            if (payload.data?.id) client.invalidateQueries({ queryKey: ['backtest', String(payload.data.id)] })
          }
          if (payload.event === 'engine') client.invalidateQueries({ queryKey: ['engine'] })
          if (payload.event === 'connection') {
            client.invalidateQueries({ queryKey: ['connection'] })
            client.invalidateQueries({ queryKey: ['engine'] })
          }
          if (['connection', 'engine', 'signal', 'trade_update'].includes(payload.event || '')) {
            client.invalidateQueries({ queryKey: ['dashboard'] })
          }
        } catch {
          // Ignore malformed or forward-compatible event payloads.
        }
      }
      socket.onclose = (event) => {
        if (stopped || event.code === 4401) return
        const delay = Math.min(30_000, 1000 * 2 ** attempts)
        attempts += 1
        retryTimer = window.setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      stopped = true
      if (retryTimer) window.clearTimeout(retryTimer)
      socket?.close()
    }
  }, [client])

  const connection = useQuery({
    queryKey: ['connection'],
    queryFn: () => api<ConnectionStatus>('/api/connection'),
    refetchInterval: 15000,
  })
  const me = useQuery({ queryKey: ['auth-me'], queryFn: () => api<AuthUser>('/api/auth/me') })
  const logout = useMutation({
    mutationFn: () => api<void>('/api/auth/logout', { method: 'POST' }),
    onSuccess: () => {
      client.clear()
      window.location.reload()
    },
  })
  const connectionView = connectionPresentation(connection.data, Boolean(connection.error))

  return (
    <div className="app-shell">
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Orbit size={24} /></div>
          <div>
            <strong>QUANTPILOT</strong>
            <span>QUANT CONTROL</span>
          </div>
        </div>
        <div className="paper-seal"><Activity size={14} /> ALPACA PAPER ONLY</div>
        <nav>
          {[...navigation, ...(me.data?.role === 'admin' ? [{ to: '/users', label: '用户管理', icon: Users }] : [])].map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => isActive ? 'nav-item active' : 'nav-item'}>
              <Icon size={18} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="system-pulse"><span /> QuantPilot 服务在线</div>
          <p>交易接口被永久锁定为模拟盘</p>
        </div>
      </aside>
      <main className="main-panel">
        <div className="topbar">
          <div className="topbar-context">
            <BarChart3 size={17} />
            <span>美股 / ETF · IEX 免费行情</span>
          </div>
          <div className="connection-status">
            <span className={`connection-dot ${connectionView.dotClass}`} />
            <Badge tone={connectionView.tone}>
              <span title={connection.data?.message || connectionView.label}>{connectionView.label}</span>
            </Badge>
            {me.data && <Badge tone={me.data.role === 'admin' ? 'info' : 'neutral'}>{me.data.role === 'admin' ? '管理员' : '用户'}</Badge>}
            <span className="topbar-user">{me.data?.username || '用户'}</span>
            <button className="button button-ghost icon-button" aria-label="退出登录" title="退出登录" onClick={() => logout.mutate()} disabled={logout.isPending}><LogOut size={15} /></button>
          </div>
        </div>
        <div className="page-container"><Outlet /></div>
      </main>
    </div>
  )
}
