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
per-user risk gate + strategy risk gate
        ↓
TradingClient(paper=True)
        ↓
TradingStream order updates
        ↓
SQLite audit trail + /ws/events
```

## 后端模块

- `alpaca_service.py`：唯一接触 Alpaca SDK 的适配层；每个用户一个实例，全部强制 Paper 模式。
- `indicators.py`：无第三方 TA 二进制依赖的 pandas 指标实现。
- `rules.py`：递归条件树与交叉条件计算。
- `backtest.py`：与实时规则解释器共用指标和条件的事件式回测；使用常数时间持仓估值、对齐基准曲线和保极值降采样。
- `risk.py`：账户、市场时段、行情新鲜度、仓位和回撤检查。
- `engine.py`：后台策略循环、流订阅、幂等信号、下单和订单对账。
- `auth.py` / `auth_api.py`：Argon2id 多用户认证、OAuth2 Password 接口、不透明会话与 CSRF 校验。
- `users_api.py`：管理员用户创建、启停、角色管理、密码重置与会话吊销。
- `runtime.py`：为每个启用用户管理独立的 Alpaca Paper 客户端与交易引擎。
- `api.py`：策略、回测、行情、风险和引擎控制接口。

## 认证边界

首次启动且数据库中不存在管理员时，公开初始化页允许创建固定 `id=1` 的初始管理员。数据库唯一约束保证并发初始化只有一个请求成功；完成后初始化接口永久返回 409。管理员随后可创建普通用户或其他管理员，且系统始终保留至少一个启用的管理员。

密码使用 Argon2id 自带的独立随机盐进行哈希。登录通过 OAuth2 Password 表单签发 12 小时有效的不透明随机令牌，数据库只保存令牌及 CSRF 值的 SHA-256 摘要，不保存原始令牌，也不使用 JWT。浏览器会话保存在 `HttpOnly`、`SameSite=Strict` Cookie 中；写操作同时要求可读 CSRF Cookie 与 `X-CSRF-Token` 请求头。API 客户端可以使用 `Authorization: Bearer <token>`，此模式不要求 CSRF。

除健康检查、认证状态、首次初始化、登录和前端静态资源外，REST API、OpenAPI 文档和 WebSocket 都需要有效会话。WebSocket 还验证同源 `Origin`，认证失败分别使用 4401/4403 关闭码。

## 持久化

SQLite 开启 WAL、外键和 NORMAL synchronous。主要数据包括：

- 策略及不可变版本。
- 回测参数、指标、权益曲线与交易明细。
- 信号唯一键与执行状态。
- Alpaca 订单镜像。
- 最近市场K线、事件日志、风险配置和引擎状态。

每个用户的 Alpaca 密钥以独立 Fernet 密文进入数据库，解密侧车密钥保存在 `data/.credentials.key`；用户密码仅保存 Argon2id 哈希，OAuth2 令牌仅保存 SHA-256 摘要。策略、回测、信号、订单镜像、风控、引擎状态、观察列表和事件均按 `user_id` 隔离；WebSocket 也只向同一用户推送事件。容器重启后 `data/` 中的 SQLite 数据与有效会话通过卷挂载保留。

回测任务先以 `queued` 状态写入 SQLite，再由后台线程获取并缓存历史K线、执行计算并保存结果。列表接口只读取摘要字段，完整曲线和交易明细只在打开具体结果时加载；服务重启会把未完成任务标记为中断，避免任务永久卡在运行中。

## 外部部署

容器内 Uvicorn 与 Docker 发布端口统一为 `0.0.0.0:10000`。公网部署建议只让 FRP 或可信网络访问源站端口，由 Nginx 终止 HTTPS，并传递 `Host`、`X-Forwarded-For`、`X-Forwarded-Proto` 以及 WebSocket Upgrade 头。HTTPS 环境必须设置 `QUANTPILOT_COOKIE_SECURE=true`。完整示例见 `docs/DEPLOYMENT.md`。

## 失败模式

- 行情流断开：SDK线程退出并记录事件，轮询循环继续通过历史接口补齐。
- 行情过期：风险层拒绝新开仓。
- 重复消息或重启：信号唯一键阻止重复下单。
- 订单拒绝：信号更新为 `rejected` 并保留错误详情。
- 账户触发亏损或回撤限制：暂停引擎并取消开放订单。
- Alpaca 未配置：应用正常启动，但所有需要外部数据的接口返回明确错误。
