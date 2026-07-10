import { useEffect, useRef } from 'react'
import { createChart, ColorType, type IChartApi, type UTCTimestamp } from 'lightweight-charts'

export default function CandleChart({ bars }: { bars: Array<any> }) {
  const container = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)

  useEffect(() => {
    if (!container.current) return
    const chart = createChart(container.current, {
      height: 420,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#8293aa' },
      grid: { vertLines: { color: '#152134' }, horzLines: { color: '#152134' } },
      rightPriceScale: { borderColor: '#24354d' },
      timeScale: { borderColor: '#24354d', timeVisible: true },
      crosshair: { vertLine: { color: '#3df6de55' }, horzLine: { color: '#a775ff55' } },
    })
    chartRef.current = chart
    const candles = chart.addCandlestickSeries({
      upColor: '#29d9b0', downColor: '#ff647c', borderVisible: false,
      wickUpColor: '#29d9b0', wickDownColor: '#ff647c',
    })
    const volume = chart.addHistogramSeries({
      priceFormat: { type: 'volume' }, priceScaleId: '', color: '#52678366',
    })
    candles.setData(bars.map((bar) => ({
      time: Math.floor(new Date(bar.timestamp).getTime() / 1000) as UTCTimestamp,
      open: bar.open, high: bar.high, low: bar.low, close: bar.close,
    })))
    volume.setData(bars.map((bar) => ({
      time: Math.floor(new Date(bar.timestamp).getTime() / 1000) as UTCTimestamp,
      value: bar.volume,
      color: bar.close >= bar.open ? '#29d9b055' : '#ff647c55',
    })))
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
    chart.timeScale().fitContent()
    const resize = new ResizeObserver(([entry]) => chart.applyOptions({ width: entry.contentRect.width }))
    resize.observe(container.current)
    return () => { resize.disconnect(); chart.remove(); chartRef.current = null }
  }, [bars])

  return <div ref={container} className="candle-chart" />
}
