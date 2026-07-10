import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  BarChart3,
  BookOpenCheck,
  Bot,
  ChartCandlestick,
  FlaskConical,
  Gauge,
  Orbit,
  Settings,
  ShieldCheck,
} from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { api } from '../api'
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
  const connection = useQuery({
    queryKey: ['connection'],
    queryFn: () => api<any>('/api/connection'),
    refetchInterval: 15000,
  })

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
          {navigation.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => isActive ? 'nav-item active' : 'nav-item'}>
              <Icon size={18} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="system-pulse"><span /> 系统本地运行</div>
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
            <span className={connection.data?.connected ? 'connection-dot online' : 'connection-dot'} />
            <Badge tone={connection.data?.connected ? 'success' : 'warning'}>
              {connection.data?.connected ? '模拟盘已连接' : '等待 Alpaca 密钥'}
            </Badge>
          </div>
        </div>
        <div className="page-container"><Outlet /></div>
      </main>
    </div>
  )
}
