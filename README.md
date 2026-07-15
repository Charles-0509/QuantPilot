# QuantPilot — Alpaca 模拟盘量化交易平台

QuantPilot 是一个完全本地运行的美股/ETF量化交易程序。它提供中文 Web UI、条件卡片规则编辑器、历史回测、用户级风险控制和 Alpaca Paper Trading 自动执行。

> 安全边界：程序中的 `TradingClient` 和 `TradingStream` 永久使用 `paper=True`，前端、API 和配置均不存在实盘切换入口。

## 功能

- 深色未来科技风格中文控制台。
- 5分钟、15分钟、30分钟、1小时和日线策略。
- 递归 AND/OR/NOT 条件树与 JSON 版本管理。
- SMA、EMA、RSI、MACD、布林带、ATR、ROC、最高/最低价、成交量均线和偏离率。
- SMA趋势、RSI均值回归、布林带、MACD、Donchian、量价突破和定投模板。
- GOOGL 专用研究模板：日线趋势突破、15分钟趋势回调、15分钟量价突破与日线布林带回归；默认禁用，需复制、回测后再启用。
- 下一根K线成交的事件式回测，支持滑点、限价、止损止盈和保守的同K线成交顺序。
- 回测时可覆盖策略股票池，快速测试 GOOGL、AAPL、NVDA 等不同标的，并与 SPY、QQQ、IWM、DIA、VTI 等常用ETF比较。
- 回测任务后台异步运行，历史列表只加载摘要；高频净值曲线自动保留极值并降采样，减少页面卡顿。
- Alpaca IEX 行情、Paper 账户、持仓、订单和成交状态同步。
- Alpaca 读取请求具备明确超时、指数退避重试、交易/行情独立断路器与自动半开恢复；短时网络故障不再要求手动重启引擎。
- 连接状态区分未配置、待探测、已连接、降级和断路保护；上游不可用时暂停新订单，并把无法确认的数据标为“未知”而不是误报为零余额、空仓或休市。
- 订单提交使用确定性的 `client_order_id` 对账；响应丢失时先恢复远端订单，不盲目重复提交。
- 单股票仓位、总暴露、最大持仓数、单日亏损和日内回撤熔断。
- SQLite 持久化、Alembic 基线迁移、信号幂等和订单对账。
- 管理员创建用户；每个用户独立拥有 Alpaca Paper 凭据、策略、回测、风控、观察列表、日志与交易引擎。
- 全部界面时间与行情图表固定使用 UTC+8（Asia/Shanghai），不受浏览器所在时区影响。
- 策略只管理自己成交形成的仓位；手动持仓和其他策略持仓不会被策略离场信号整仓关闭。
- 持续核对 Alpaca 实际持仓、开放卖单与本地策略归属；检测到手工卖出、卖单超量、实际持仓不足或空头时，立即暂停引擎并进入持久化执行安全隔离。
- 风控同时计算已成交持仓、未成交买单和本次拟下单金额，并在拒绝日志中显示全局/策略两层限制。

## 一键部署（Debian/Ubuntu）

```bash
curl -fsSL https://raw.githubusercontent.com/Charles-0509/QuantPilot/main/scripts/install.sh | sudo bash
```

安装器会自动安装或检查 Docker，询问安装目录（默认 `/opt/quantpilot`）和访问端口（默认 `10000`），随后部署公开的 AMD64/ARM64 镜像。完成后通过 `http://服务器IP:端口` 打开首次初始化页面并创建管理员。已有安装可使用同一命令修复，或在二次确认后删除。

安装后可使用：

```bash
quan update
quan upgrade
quan status
quan logs
quan restart
quan start
quan stop
```

`quan update` 只比较运行版本与 GitHub 最新稳定标签；有更新时提示运行 `quan upgrade`，不会主动拉取镜像。

## 从仓库启动

1. 首次使用时复制环境文件（不需要在其中填写密钥）：

```bash
cp .env.example .env
```

2. 启动：

```bash
docker compose pull
docker compose up -d
```

3. 打开 [http://localhost:10000](http://localhost:10000)。首次启动先创建初始管理员；管理员可在“用户管理”创建其他账户。每位用户登录后，在 Alpaca Dashboard 切换到 **Paper Account**，进入“设置”填写自己的 API Key 与 Secret。网页配置会以密文保存到本机 SQLite 数据库；解密密钥仅保存在同一台机器的 `data/.credentials.key`，请保护 Docker 的 `data/` 目录。

4. `.env` 仅作为原始管理员 `id=1` 的兼容后备配置；其他用户必须在网页中填写自己的凭据：

```env
APCA_API_KEY_ID=你的模拟盘Key
APCA_API_SECRET_KEY=你的模拟盘Secret
ALPACA_DATA_FEED=iex
```

Docker 默认将 `10000` 端口发布到 `0.0.0.0`，可通过 `HTTP + IP` 直接访问，也可由 Nginx/Caddy 提供 `HTTPS + 域名/IP`。默认 `QUANTPILOT_COOKIE_SECURE=false` 兼容两种入口；公网 HTTP 会明文传输密码和会话，不应在不可信网络使用。仅允许 HTTPS 访问时可设置 `QUANTPILOT_COOKIE_SECURE=true`。网页只会在该用户没有策略归属持仓、未结 QuantPilot 订单、待对账订单意图和活动执行隔离时允许更换 Alpaca 凭据；更新成功后引擎进入安全暂停状态，需在“自动交易”页面确认后重新启动。管理员网页配置优先于 `.env`；普通用户没有共享的 `.env` 后备凭据。

也可以直接拉取 AMD64/ARM64 公共镜像：`ghcr.io/charles-0509/quantpilot:1.4.0` 或 `ghcr.io/charles-0509/quantpilot:latest`。

已有部署在线升级（Alembic 会自动保留数据并把原有记录归到管理员）：

```bash
quan update
quan upgrade
```

`quan upgrade` 会拉取最新稳定镜像、等待 `/api/health` 验证成功、更新 `quan` 命令并清理旧的 QuantPilot 镜像；不会删除数据库、用户配置、策略或交易记录。升级到 1.4.0 时 Alembic 自动迁移到 `0006_execution_incidents`。Alpaca 短时 TLS 断流、连接超时、HTTP 429 与 5xx 会自动重试，连续失败时进入断路保护并在冷却后自动恢复。

## Alpaca 连接状态

- `未配置（unconfigured）`：当前用户尚未填写 Alpaca Paper API Key 与 Secret。
- `待探测（unknown）`：凭据已配置，但本次进程尚未完成一次成功或失败的 Alpaca 请求。
- `已连接（connected）`：最近的交易与行情链路正常，引擎可以接受新订单。
- `降级（degraded）`：出现可恢复的连接错误；系统自动重试并安全跳过无法取得完整快照的策略轮次。
- `断路保护（circuit_open）`：连续失败达到阈值后暂时停止访问故障通道，默认30秒后自动半开探测；成功后无需人工重启即可恢复。

“交易引擎已开启”表示用户希望自动交易持续运行；只有运行状态同时显示“可接受新订单”时才会创建订单。上游故障期间，应用自身 `/api/health` 仍保持正常，避免 Docker 因第三方网络波动反复重启。可选的超时、重试、断路器和缓存环境变量见 [部署说明](docs/DEPLOYMENT.md#alpaca-连接可靠性参数)。

如果你在 Alpaca 端手工卖出 QuantPilot 正在管理的股票，或其他程序创建了可能导致超卖/空头的订单，QuantPilot 会优先停止自动执行，而不是猜测仓位归属。系统会取消自己创建的相关订单、清除不再可信的策略归属并使用 Alpaca REST 复核；确认没有开放卖单且没有空头后，隔离才会转为已控制状态，仍需用户检查实际持仓后手工恢复引擎。

## 推荐使用顺序

1. 从策略库复制模板。
2. 在规则编辑器中修改股票池、条件、仓位与风控。
3. 在回测实验室使用至少一个完整市场阶段验证。
4. 在策略库启用策略。
5. 在自动交易页启动引擎。
6. 观察一段时间模拟盘行为，再决定是否继续改进规则。

## 开发与测试

前端：

```bash
cd frontend
npm install
npm run build
npm test
```

后端测试建议在 Python 3.12 容器中执行：

```bash
docker compose run --rm quantpilot pytest -q
```

登录后的 API 文档位于 [http://localhost:10000/docs](http://localhost:10000/docs)。FRP、Nginx 与 HTTPS 配置参见 [部署说明](docs/DEPLOYMENT.md)。

## 数据与模拟限制

- 免费 IEX 并不代表全美交易所 SIP 全市场成交量。
- 免费实时订阅最多使用30个股票代码；应用在启用策略时会检查。
- Alpaca Paper 不完整模拟市场冲击、订单排队、真实延迟滑点、费用和分红。
- 本程序不是投资建议，历史回测和模拟盘收益不代表未来表现。

详细资料参见 [架构说明](docs/ARCHITECTURE.md) 与 [规则格式](docs/RULES.md)。
