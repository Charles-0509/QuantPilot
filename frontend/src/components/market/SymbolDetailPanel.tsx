import { formatTime } from '../../api'
import type { SymbolDetail } from '../../types'

function compact(value: number | null | undefined) {
  if (value == null) return '—'
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}K`
  return value.toFixed(0)
}

const price = (value: number | null | undefined) => value == null ? '—' : value.toFixed(2)
const SESSION: Record<string, string> = { pre: '盘前', regular: '盘中', post: '盘后', closed: '已收盘' }

export default function SymbolDetailPanel({ detail, loading, error, inWatchlist, onAdd }: {
  detail?: SymbolDetail
  loading: boolean
  error?: string
  inWatchlist: boolean
  onAdd: () => void
}) {
  if (loading && !detail) return <section className="symbol-detail"><p className="field-hint">正在读取股票信息…</p></section>
  if (!detail) return <section className="symbol-detail"><p className="field-hint">{error || '暂无数据'}</p></section>
  const up = (detail.change ?? 0) >= 0
  const rangePct = detail.high_52w != null && detail.low_52w != null && detail.price != null && detail.high_52w > detail.low_52w
    ? Math.min(100, Math.max(0, (detail.price - detail.low_52w) / (detail.high_52w - detail.low_52w) * 100)) : null
  const stats: Array<[string, string, string?]> = [
    ['开盘', price(detail.open)], ['昨收', price(detail.prev_close)],
    ['最高', price(detail.high)], ['最低', price(detail.low)],
    ['昨日成交量', compact(detail.prev_volume), '全市场 SIP'], ['30日均量', compact(detail.avg_volume_30), '全市场 SIP'],
    ['今日成交量', compact(detail.volume_live), '仅 IEX，偏小'], ['52周区间', detail.low_52w != null ? `${price(detail.low_52w)} – ${price(detail.high_52w)}` : '—'],
  ]
  return <section className="symbol-detail" aria-label={`${detail.symbol}详情`}>
    <div className="symbol-detail-head">
      <div>
        <strong>{detail.symbol}</strong>
        <p>{detail.name || '—'}{detail.exchange ? ` · ${detail.exchange}` : ''}</p>
      </div>
      {!inWatchlist && <button className="button button-secondary" onClick={onAdd}>+ 加入自选</button>}
    </div>
    <div className="symbol-detail-price">
      <span className="symbol-detail-last">{price(detail.price)}</span><small>USD</small>
      <span className={up ? 'positive' : 'negative'}>{detail.change == null ? '—' : `${up ? '+' : ''}${detail.change.toFixed(2)}`}</span>
      <span className={up ? 'positive' : 'negative'}>{detail.change_pct == null ? '' : `${up ? '+' : ''}${detail.change_pct.toFixed(2)}%`}</span>
    </div>
    <p className="field-hint">{detail.session ? `${SESSION[detail.session]} · ` : ''}最后成交 {formatTime(detail.price_time)}{detail.live_feed ? ' · IEX 实时' : ' · 日线收盘价'}</p>
    <div className="symbol-detail-stats" title="涨跌相对上一交易日收盘价。昨收、均量和 52 周区间来自全市场 SIP 日线；最新价和今日数据来自 IEX 实时行情。">
      {stats.map(([label, value, hint]) => <div key={label}><span title={hint}>{label}</span><b>{value}</b></div>)}
    </div>
    {rangePct != null && <div className="symbol-range" title="当前价格在 52 周区间中的位置">
      <span style={{ left: `${rangePct}%` }} />
    </div>}
  </section>
}
