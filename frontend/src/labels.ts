// Signal action labels shared by the dashboard and trading pages.
const ACTION_LABELS: Record<string, string> = { buy: '买入', sell: '卖出', short: '卖出开空', cover: '买入平空' }
export function actionLabel(action: string) {
  return ACTION_LABELS[action] ?? action
}
export function actionTone(action: string): 'success' | 'warning' {
  return action === 'buy' || action === 'cover' ? 'success' : 'warning'
}
