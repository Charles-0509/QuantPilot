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
- 限价买单在信号后才可成交，成交价不超过买入限价。DAY 单在首个可执行交易日结束后失效；GTC 单可跨日等待。
- 同一K线同时触及止损和止盈时优先止损。
- 回测不模拟部分成交和订单队列，默认佣金为0、滑点为5基点。
- 预热行情在回测开始日期之前读取；预热不足时不生成信号。区间之前的行情不计入收益，也不会生成待成交订单。
- ATR保护价使用信号K线的已知指标，并分别遵守止损/止盈的ATR周期；不会读取成交当根K线未来的最高最低价。
- 回测遵守策略交易星期、最大持仓数、每标的冷却K线，并冻结当前用户的全局仓位、敞口、日亏损与日内回撤限制。触发账户熔断后，本次回测停止新信号，已有保护单仍可执行。
- Bracket、移动止损和GTC入场采用整股数量，并向下取整，不突破仓位预算。普通DAY单支持六位小数股数。
- 移动止损采用上一根已完成K线的最高水位；本根最高价抬升的止损从下一根起生效。这是明确的OHLC路径近似，不模拟逐笔成交。
- Sortino以0为最低可接受收益，用全部周期计算下行偏差；分母为零的Sharpe、Sortino、盈亏比与Profit Factor显示“—”，不以999代表无限值。CAGR按实际经过时间年化。
- 期末持仓按最后收盘价并计入滑点、佣金模拟平仓；该笔交易纳入胜率与交易数。基准缺失起始价格时会明确报错，不用未来价格回填。
- 回测实验室可以临时覆盖 `symbols`，把同一套指标和交易规则应用到其他股票或ETF；此覆盖只属于该次回测，不会修改或启用原策略。
- 基准可选择 SPY、QQQ、IWM、DIA、VTI、VOO、RSP 或 XLK，用于比较同期买入持有表现。

## 实验快照

新回测绑定规则定义、策略版本、风险配置，保存压缩的完整输入行情、数据SHA-256及计算代码/依赖标识。结果页可下载自己的实验快照；旧记录不会伪造缺失数据。回测列表不加载大体积行情。

使用与快照一致的代码和Python依赖，在离线环境复跑：

```bash
.venv/bin/python scripts/replay-backtest.py backtest-<id>.json.gz
```

脚本不访问券商或数据库；行情校验和、代码或依赖版本不一致会拒绝静默复现。快照包含私有策略定义，分享前应检查内容。
