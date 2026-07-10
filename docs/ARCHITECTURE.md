# 架构说明

## 运行结构

生产镜像先使用 Node 构建 React UI，再把静态文件复制到 Python 3.12 镜像，由一个 FastAPI 进程同时提供 UI、REST API、WebSocket 和交易引擎。

单进程是明确的安全要求：如果启动多个 Uvicorn Worker，每个 Worker 都可能启动自己的策略循环并造成重复下单。信号表仍使用唯一键作为第二道保护。

## 数据流

```text
Alpaca IEX minute stream
        ↓
completed-bar trigger + REST catch-up
        ↓
indicator calculator / RuleDefinition evaluator
        ↓
global risk gate + strategy risk gate
        ↓
TradingClient(paper=True)
        ↓
TradingStream order updates
        ↓
SQLite audit trail + /ws/events
```

## 后端模块

- `alpaca_service.py`：唯一接触 Alpaca SDK 的适配层，强制 Paper 模式。
- `indicators.py`：无第三方 TA 二进制依赖的 pandas 指标实现。
- `rules.py`：递归条件树与交叉条件计算。
- `backtest.py`：与实时规则解释器共用指标和条件的事件式回测。
- `risk.py`：账户、市场时段、行情新鲜度、仓位和回撤检查。
- `engine.py`：后台策略循环、流订阅、幂等信号、下单和订单对账。
- `api.py`：策略、回测、行情、风险和引擎控制接口。

## 持久化

SQLite 开启 WAL、外键和 NORMAL synchronous。主要数据包括：

- 策略及不可变版本。
- 回测参数、指标、权益曲线与交易明细。
- 信号唯一键与执行状态。
- Alpaca 订单镜像。
- 最近市场K线、事件日志、风险配置和引擎状态。

密钥不进入数据库。容器重启后 `data/` 中的 SQLite 数据通过卷挂载保留。

## 失败模式

- 行情流断开：SDK线程退出并记录事件，轮询循环继续通过历史接口补齐。
- 行情过期：风险层拒绝新开仓。
- 重复消息或重启：信号唯一键阻止重复下单。
- 订单拒绝：信号更新为 `rejected` 并保留错误详情。
- 账户触发亏损或回撤限制：暂停引擎并取消开放订单。
- Alpaca 未配置：应用正常启动，但所有需要外部数据的接口返回明确错误。
