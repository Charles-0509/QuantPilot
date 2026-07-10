# RuleDefinition v1

每个策略使用一份可版本化 JSON。前端条件卡片只是该格式的可视化编辑器。

## 条件树

组节点：

```json
{
  "type": "group",
  "op": "AND",
  "negate": false,
  "children": []
}
```

条件节点：

```json
{
  "type": "condition",
  "left": {
    "kind": "price",
    "field": "close"
  },
  "operator": "crosses_above",
  "right": {
    "kind": "indicator",
    "indicator": "SMA",
    "field": "value",
    "params": { "period": 20 }
  },
  "label": "价格上穿 SMA20"
}
```

操作数类型：

- `price`：open、high、low、close、volume。
- `indicator`：指标、参数、输出字段和历史偏移。
- `number`：常数。

比较符：`>`、`>=`、`<`、`<=`、`==`、`crosses_above`、`crosses_below`。

## 指标输出

- SMA、EMA、RSI、ATR、ROC、HIGHEST、LOWEST、VOLUME_SMA、DEVIATION：`value`。
- MACD：`macd`、`signal`、`histogram`。
- BOLLINGER：`upper`、`middle`、`lower`。
- HIGHEST/LOWEST 默认 `exclude_current=true`，突破条件不会把当前K线加入历史极值。

## 仓位

- `percent_equity`：账户净值百分比。
- `fixed_notional`：固定美元金额。
- `fixed_qty`：固定股数。
- `risk_based`：将 `value` 视为账户风险百分比，根据止损距离计算数量。

`allow_pyramiding` 控制是否允许已有持仓继续买入，`max_additions` 限制追加次数。

## 订单与保护

- 市价订单直接提交。
- 限价买单使用信号价格减去 `limit_offset_bps`。
- 同时设置 stop_loss 和 take_profit 时创建 Alpaca bracket 订单。
- trailing_stop 与 bracket 互斥；移动止损在买单完全成交后单独提交。
- 扩展时段固定关闭，TIF 只允许 DAY 或 GTC。

## 回测约定

- 当前K线收盘后才能产生信号。
- 市价信号在下一根K线开盘成交。
- 限价买单只在下一根K线触及限价时成交，否则取消。
- 同一K线同时触及止损和止盈时优先止损。
- 回测不模拟部分成交和订单队列，默认佣金为0、滑点为5基点。
- 回测实验室可以临时覆盖 `symbols`，把同一套指标和交易规则应用到其他股票或ETF；此覆盖只属于该次回测，不会修改或启用原策略。
- 基准可选择 SPY、QQQ、IWM、DIA、VTI、VOO、RSP 或 XLK，用于比较同期买入持有表现。
