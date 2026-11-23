import { useEffect, useMemo, useRef, useState } from 'react'
import { createChart, ColorType, CrosshairMode, type IChartApi, type ISeriesApi, type Time, type UTCTimestamp } from 'lightweight-charts'
import { formatShanghaiChartTick, formatShanghaiDateTime, formatTradingDate, parseUtcDate } from '../../time'
import { MA_LINES, sma, type Bar } from './indicators'

const UP = '#26c6a2'
const DOWN = '#f2566f'

function toTime(bar: Bar) {
  return Math.floor(parseUtcDate(bar.timestamp).getTime() / 1000) as UTCTimestamp
}

function compact(value: number) {
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}K`
  return value.toFixed(0)
}

export default function MarketChart({ bars, viewKey, visibleMa, daily = false }: {
  bars: Bar[]
  viewKey: string
  visibleMa: Record<number, boolean>
  daily?: boolean
}) {
  const container = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<{ candles: ISeriesApi<'Candlestick'>; volume: ISeriesApi<'Histogram'>; ma: Record<number, ISeriesApi<'Line'>> } | null>(null)
  const fittedKey = useRef('')
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const averages = useMemo(() => Object.fromEntries(MA_LINES.map(({ period }) => [period, sma(bars.map((bar) => bar.close), period)])), [bars])
  const indexByTime = useMemo(() => new Map(bars.map((bar, index) => [toTime(bar), index])), [bars])
  const indexRef = useRef(indexByTime)
  indexRef.current = indexByTime
  const dailyRef = useRef(daily)
  dailyRef.current = daily

  useEffect(() => {
    if (!container.current) return
    const chart = createChart(container.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#8a9ab0', fontSize: 11 },
      grid: { vertLines: { color: 'rgba(120,140,170,.07)' }, horzLines: { color: 'rgba(120,140,170,.07)' } },
      rightPriceScale: { borderColor: 'rgba(120,140,170,.2)', scaleMargins: { top: 0.08, bottom: 0.24 } },
      timeScale: { borderColor: 'rgba(120,140,170,.2)', timeVisible: true, rightOffset: 6,
        tickMarkFormatter: (time: Time, type: number) => dailyRef.current
          ? (type === 0 ? formatTradingDate(Number(time)).slice(0, 4) : formatTradingDate(Number(time), false))
          : formatShanghaiChartTick(Number(time)) },
      localization: { timeFormatter: (time: Time) => dailyRef.current ? formatTradingDate(Number(time)) : formatShanghaiDateTime(Number(time) * 1000) },
      crosshair: { mode: CrosshairMode.Normal },
    })
    const candles = chart.addCandlestickSeries({ upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN })
    const volume = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'volume', lastValueVisible: false, priceLineVisible: false })
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
    const ma = Object.fromEntries(MA_LINES.map(({ period, color }) => [period, chart.addLineSeries({
      color, lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false, lastValueVisible: true,
    })])) as Record<number, ISeriesApi<'Line'>>
    chart.subscribeCrosshairMove((param) => {
      const index = param.time === undefined ? undefined : indexRef.current.get(param.time as UTCTimestamp)
      setHoverIndex(index ?? null)
    })
    chartRef.current = chart
    seriesRef.current = { candles, volume, ma }
    return () => { chart.remove(); chartRef.current = null; seriesRef.current = null; fittedKey.current = '' }
  }, [])

  useEffect(() => {
    const series = seriesRef.current
    if (!series) return
    series.candles.setData(bars.map((bar) => ({ time: toTime(bar), open: bar.open, high: bar.high, low: bar.low, close: bar.close })))
    series.volume.setData(bars.map((bar) => ({ time: toTime(bar), value: bar.volume, color: bar.close >= bar.open ? `${UP}66` : `${DOWN}66` })))
    MA_LINES.forEach(({ period }) => series.ma[period].setData(bars.flatMap((bar, index) => {
      const value = averages[period][index]
      return value == null ? [] : [{ time: toTime(bar), value }]
    })))
    if (bars.length && fittedKey.current !== viewKey) {
      // New symbol/timeframe: show the latest ~150 bars; refreshes keep the user's zoom.
      chartRef.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, bars.length - 150), to: bars.length + 5 })
      fittedKey.current = viewKey
    }
  }, [bars, averages, viewKey])

  useEffect(() => { chartRef.current?.applyOptions({ timeScale: { timeVisible: !daily } }) }, [daily])

  useEffect(() => {
    MA_LINES.forEach(({ period }) => seriesRef.current?.ma[period].applyOptions({ visible: visibleMa[period] }))
  }, [visibleMa])

  const index = hoverIndex ?? bars.length - 1
  const bar = bars[index]
  const previous = bars[index - 1]
  const change = bar && previous ? bar.close - previous.close : null
  return <div className="market-chart">
    <div ref={container} className="market-chart-canvas" />
    {bar && <div className="market-legend">
      <div className="market-legend-row">
        <span>开 <b className={bar.close >= bar.open ? 'positive' : 'negative'}>{bar.open.toFixed(2)}</b></span>
        <span>高 <b className={bar.close >= bar.open ? 'positive' : 'negative'}>{bar.high.toFixed(2)}</b></span>
        <span>低 <b className={bar.close >= bar.open ? 'positive' : 'negative'}>{bar.low.toFixed(2)}</b></span>
        <span>收 <b className={bar.close >= bar.open ? 'positive' : 'negative'}>{bar.close.toFixed(2)}</b></span>
        {change != null && <b className={change >= 0 ? 'positive' : 'negative'}>{change >= 0 ? '+' : ''}{change.toFixed(2)} ({(change / previous.close * 100).toFixed(2)}%)</b>}
      </div>
      <div className="market-legend-row">
        <span>成交量 <b>{compact(bar.volume)}</b></span>
        {MA_LINES.filter(({ period }) => visibleMa[period]).map(({ period, color }) => <span key={period}>MA{period} <b style={{ color }}>{averages[period][index]?.toFixed(2) ?? '—'}</b></span>)}
      </div>
    </div>}
  </div>
}
