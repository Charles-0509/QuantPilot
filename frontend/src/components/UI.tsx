import type { ButtonHTMLAttributes, HTMLAttributes, PropsWithChildren, ReactNode } from 'react'
import { AlertTriangle, LoaderCircle, TrendingDown, TrendingUp } from 'lucide-react'

export function Card({ children, className = '', ...props }: PropsWithChildren<HTMLAttributes<HTMLElement>>) {
  return <section className={`glass-card ${className}`} {...props}>{children}</section>
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        {description && <p className="page-description">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  )
}

export function Badge({
  children,
  tone = 'neutral',
}: PropsWithChildren<{ tone?: 'success' | 'warning' | 'danger' | 'info' | 'neutral' }>) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}

export function Button({
  children,
  variant = 'primary',
  className = '',
  ...props
}: PropsWithChildren<
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'danger' | 'ghost' }
>) {
  return (
    <button className={`button button-${variant} ${className}`} {...props}>
      {children}
    </button>
  )
}

export function StatCard({
  label,
  value,
  detail,
  trend,
  icon,
}: {
  label: string
  value: string
  detail?: string
  trend?: number
  icon?: ReactNode
}) {
  return (
    <Card className="stat-card">
      <div className="stat-topline">
        <span>{label}</span>
        <span className="stat-icon">{icon}</span>
      </div>
      <div className="stat-value">{value}</div>
      <div className="stat-detail">
        {trend !== undefined && (
          <span className={trend >= 0 ? 'positive' : 'negative'}>
            {trend >= 0 ? <TrendingUp size={14} /> : <TrendingDown size={14} />}
            {Math.abs(trend).toFixed(2)}%
          </span>
        )}
        {detail && <span>{detail}</span>}
      </div>
    </Card>
  )
}

export function Loading({ label = '正在读取数据' }: { label?: string }) {
  return (
    <div className="loading-state">
      <LoaderCircle className="spin" size={22} /> {label}
    </div>
  )
}

export function ErrorPanel({ message }: { message: string }) {
  return (
    <Card className="error-panel">
      <AlertTriangle size={20} />
      <div>
        <strong>暂时无法完成请求</strong>
        <p>{message}</p>
      </div>
    </Card>
  )
}

export function Empty({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="empty-state">
      <div className="empty-orb" />
      <strong>{title}</strong>
      {detail && <p>{detail}</p>}
    </div>
  )
}

export function Field({
  label,
  hint,
  children,
}: PropsWithChildren<{ label: string; hint?: string }>) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  )
}
