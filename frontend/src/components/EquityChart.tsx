import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

export default function EquityChart({
  equity,
  benchmark = [],
}: {
  equity: Array<{ timestamp: string; equity: number }>
  benchmark?: Array<{ timestamp: string; equity: number }>
}) {
  const benchmarkMap = new Map(benchmark.map((item) => [item.timestamp, item.equity]))
  const data = equity.map((item) => ({
    ...item,
    label: new Date(item.timestamp).toLocaleDateString('zh-CN'),
    benchmark: benchmarkMap.get(item.timestamp),
  }))
  return (
    <div className="chart-frame">
      <ResponsiveContainer width="100%" height={310}>
        <AreaChart data={data} margin={{ top: 12, right: 8, left: 8, bottom: 0 }}>
          <defs>
            <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#3df6de" stopOpacity={0.35} />
              <stop offset="100%" stopColor="#3df6de" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#172437" strokeDasharray="3 7" vertical={false} />
          <XAxis dataKey="label" stroke="#60748f" tickLine={false} axisLine={false} minTickGap={40} />
          <YAxis stroke="#60748f" tickLine={false} axisLine={false} width={70} />
          <Tooltip contentStyle={{ background: '#0b1320', border: '1px solid #24354d', borderRadius: 12 }} />
          <Area type="monotone" dataKey="equity" stroke="#3df6de" strokeWidth={2} fill="url(#equityFill)" />
          {benchmark.length > 0 && <Area type="monotone" dataKey="benchmark" stroke="#a775ff" fill="transparent" strokeWidth={1.5} />}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
